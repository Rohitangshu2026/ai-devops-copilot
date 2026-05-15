"""Deployment-aware incident correlation.

Looks at the Kubernetes Deployment / ReplicaSet rollout history for a
service and asks one question: *did a recent rollout precede the error
spike?*  When the answer is yes, the analysis pipeline gets two new
fields in its response:

  ``deployment_timeline``     — every rollout in the correlation window
  ``rollback_candidate``      — populated only when the heuristic AND
                                 the safety gate both fire.

This module is **read-only**.  It never triggers a rollout.  The
recommendation is surfaced through the standard ``AnalysisResult`` so an
operator (or the existing approval workflow) can act on it.

Design notes
------------

* Reuses the same kubernetes client pattern as ``blast_radius.py`` and
  ``llm/tools.py`` — in-cluster config first, kubeconfig fallback,
  graceful return on ImportError / ApiException.

* No new dependency.  No new framework.  Pure stdlib + ``kubernetes``.

* The k8s response is cached for 30s per ``(namespace, service)`` tuple
  so that repeat analysis calls during a polling loop don't hammer the
  API server.

* The heuristic is deliberately conservative — false positives (suggest a
  rollback when one isn't warranted) erode trust faster than false
  negatives.  We require:
      a) a rollout within the window, AND
      b) the change-point lies AFTER the rollout, AND
      c) the gap between rollout and change-point is plausible
         (≤ correlation_window).

  Without a clean change-point in the log summary we degrade to "any
  rollout in the window is suspect" but never auto-generate a rollback
  candidate from that weaker signal.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger("deployment_correlation")

# ── Tunables (env-overridable for ops) ───────────────────────────────────────

_CACHE_TTL_SECONDS = 30.0

_DEFAULT_WINDOW_MINUTES = int(os.getenv("DEPLOYMENT_CORRELATION_WINDOW_MIN", "15"))
_DEFAULT_CONFIDENCE_THRESHOLD = int(os.getenv("ROLLBACK_CANDIDATE_MIN_SCORE", "7"))
# Production-like namespaces: rollback candidates are NEVER generated here.
# The recommendation surfaces only in non-production environments.  Override
# via env (comma-separated).
_PROD_NAMESPACES = {
    s.strip().lower()
    for s in os.getenv("PRODUCTION_NAMESPACES", "production,prod").split(",")
    if s.strip()
}


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class DeploymentEvent:
    """One rolled-out Deployment within the correlation window."""

    deployment_name: str
    image_tag: str
    rolled_out_at: str               # ISO8601 UTC
    deployment_age_minutes: float    # how long ago the rollout completed
    minutes_before_incident: Optional[float] = None   # set when change_point known


@dataclass
class RollbackCandidate:
    """A non-binding recommendation.  Never auto-executed."""

    deployment: str
    current_image: str
    recommended_action: str = "rollback"
    reason: str = ""
    blast_radius_score: str = "unknown"      # populated from existing blast_radius
    auto_executable: bool = False            # explicit — operator must confirm


@dataclass
class DeploymentCorrelationResult:
    deployment_timeline: List[DeploymentEvent] = field(default_factory=list)
    deployment_suspected: bool = False
    rollback_candidate: Optional[RollbackCandidate] = None
    source: str = "unknown"                  # k8s_api | unavailable | error
    correlation_window_minutes: int = _DEFAULT_WINDOW_MINUTES

    def to_response_dict(self) -> Dict[str, Any]:
        """Plain-dict view for AnalysisResult and the dashboard."""
        return {
            "deployment_timeline": [asdict(d) for d in self.deployment_timeline],
            "deployment_suspected": self.deployment_suspected,
            "rollback_candidate": asdict(self.rollback_candidate) if self.rollback_candidate else None,
            "source": self.source,
            "correlation_window_minutes": self.correlation_window_minutes,
        }


# ── Cache ────────────────────────────────────────────────────────────────────


_cache: Dict[Tuple[str, str], Tuple[float, List[DeploymentEvent]]] = {}


def _cache_get(namespace: str, service: str) -> Optional[List[DeploymentEvent]]:
    entry = _cache.get((namespace, service))
    if entry is None:
        return None
    fetched_at, events = entry
    if (time.monotonic() - fetched_at) < _CACHE_TTL_SECONDS:
        return events
    return None


def _cache_put(namespace: str, service: str, events: List[DeploymentEvent]) -> None:
    _cache[(namespace, service)] = (time.monotonic(), events)


# ── k8s lookup ───────────────────────────────────────────────────────────────


def _matches_service(deployment_name: str, deployment_labels: Dict[str, str], service: str) -> bool:
    """Tolerant match: deployment name == service OR app/service label == service."""
    if deployment_name == service:
        return True
    for label_key in ("app", "app.kubernetes.io/name", "devops-copilot/service"):
        if deployment_labels.get(label_key) == service:
            return True
    return False


def _extract_rollout_timestamp(deployment) -> Optional[datetime]:
    """Pick the most reliable rollout timestamp for a Deployment.

    Priority:
      1. ``Progressing`` condition's ``last_update_time`` (set when the
         rolling update last advanced — e.g. a new ReplicaSet became active)
      2. ``Available`` condition's ``last_update_time`` (when all replicas
         became ready) — slightly later than the actual rollout, but still
         useful when ``Progressing`` is absent on managed clusters
      3. ``status.conditions[*].last_transition_time`` newest
      4. Deployment ``creation_timestamp`` (last resort — the deployment
         itself may pre-date the rollout we care about)
    """
    conditions = deployment.status.conditions or []
    for target_type in ("Progressing", "Available"):
        for c in conditions:
            if c.type == target_type and c.last_update_time:
                ts = c.last_update_time
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    # Newest transition across all conditions
    candidates = [
        (c.last_transition_time if c.last_transition_time.tzinfo
         else c.last_transition_time.replace(tzinfo=timezone.utc))
        for c in conditions
        if c.last_transition_time
    ]
    if candidates:
        return max(candidates)
    ts = deployment.metadata.creation_timestamp
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _fetch_deployments(namespace: str, service: str) -> Optional[List[DeploymentEvent]]:
    """Read recent Deployment rollouts in *namespace* that match *service*.

    Returns:
        List[DeploymentEvent] when the API call succeeded.  Empty list when
        no match was found (still a successful lookup).  ``None`` when the
        API itself is unreachable (graceful degradation — surfaces as
        ``source="unavailable"`` in the result).
    """
    cached = _cache_get(namespace, service)
    if cached is not None:
        return cached

    try:
        # Soft import — the kubernetes package is optional in dev.
        try:
            from kubernetes import client as k8s_client, config as k8s_config
        except ImportError:
            return None

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            try:
                k8s_config.load_kube_config()
            except Exception:  # noqa: BLE001
                return None

        apps_v1 = k8s_client.AppsV1Api()
        resp = apps_v1.list_namespaced_deployment(
            namespace=namespace, _request_timeout=5,
        )

        events: List[DeploymentEvent] = []
        now = datetime.now(timezone.utc)
        for dep in resp.items:
            labels = (dep.metadata.labels or {})
            if not _matches_service(dep.metadata.name, labels, service):
                continue

            rollout_ts = _extract_rollout_timestamp(dep)
            if rollout_ts is None:
                continue

            image_tag = ""
            try:
                image_tag = dep.spec.template.spec.containers[0].image or ""
            except (AttributeError, IndexError):
                pass

            age_min = (now - rollout_ts).total_seconds() / 60.0
            events.append(DeploymentEvent(
                deployment_name=dep.metadata.name,
                image_tag=image_tag,
                rolled_out_at=rollout_ts.isoformat(),
                deployment_age_minutes=round(age_min, 2),
            ))

        _cache_put(namespace, service, events)
        return events

    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "message": "k8s_deployment_fetch_failed",
            "namespace": namespace,
            "service": service,
            "error": str(exc),
        })
        return None


# ── Public entry point ───────────────────────────────────────────────────────


def analyze_deployment_correlation(
    *,
    service: str,
    namespace: str,
    incident_summary: Any,                      # LogSummary duck-type
    confidence_score: int,
    blast_radius_score: str = "unknown",
    is_production: Optional[bool] = None,
    correlation_window_minutes: Optional[int] = None,
    confidence_threshold: Optional[int] = None,
) -> DeploymentCorrelationResult:
    """Correlate a recent rollout with the incident's change-point.

    Parameters
    ----------
    service, namespace
        Identify the workload.
    incident_summary
        Anything that exposes ``change_point_minutes_ago`` (``LogSummary``
        does).  When ``None``, the heuristic degrades to "any rollout
        within the window is suspect" but rollback candidates are still
        gated on confidence.
    confidence_score
        From the existing confidence scorer.  Rollback candidate is gated
        on ``score >= confidence_threshold`` (default 7 ≈ "high" tier).
    blast_radius_score
        The string already computed by ``blast_radius.py`` — included in
        the candidate object so operators see impact at a glance.
    is_production
        Override the namespace-based production detection.  When None we
        check ``namespace`` against ``PRODUCTION_NAMESPACES`` (env-driven).
    correlation_window_minutes
        How far back to look for rollouts.  Default 15m.
    confidence_threshold
        Minimum ``confidence_score`` required to surface a rollback
        candidate.  Default 7.

    Returns
    -------
    DeploymentCorrelationResult
        ``source="unavailable"`` when the k8s API is unreachable — callers
        should treat the result as "no signal" and proceed normally.
    """
    window = correlation_window_minutes if correlation_window_minutes is not None else _DEFAULT_WINDOW_MINUTES
    threshold = confidence_threshold if confidence_threshold is not None else _DEFAULT_CONFIDENCE_THRESHOLD
    if is_production is None:
        is_production = namespace.strip().lower() in _PROD_NAMESPACES

    logger.info({
        "message": "deployment_analysis_started",
        "service": service,
        "namespace": namespace,
        "window_minutes": window,
        "is_production": is_production,
    })

    events = _fetch_deployments(namespace, service)
    if events is None:
        # k8s API not reachable — return an "unavailable" result so the
        # response shape is consistent.  Never propagates as an error.
        return DeploymentCorrelationResult(
            source="unavailable",
            correlation_window_minutes=window,
        )

    recent = [e for e in events if e.deployment_age_minutes <= window]

    # Pull change-point (when available) from the log summary.
    change_point_minutes_ago = None
    if incident_summary is not None:
        change_point_minutes_ago = getattr(incident_summary, "change_point_minutes_ago", None)

    # Annotate each event with minutes_before_incident.
    deployment_suspected = False
    for e in recent:
        if change_point_minutes_ago is not None:
            # Rollout happened `deployment_age_minutes` ago; change-point
            # happened `change_point_minutes_ago` ago.  Rollout came
            # FIRST when its age > change-point's age.
            if e.deployment_age_minutes > change_point_minutes_ago:
                gap = e.deployment_age_minutes - change_point_minutes_ago
                e.minutes_before_incident = round(gap, 2)
                if gap <= window:
                    deployment_suspected = True
                    logger.info({
                        "message": "deployment_correlation_detected",
                        "service": service,
                        "deployment": e.deployment_name,
                        "image": e.image_tag,
                        "minutes_before_incident": round(gap, 2),
                    })
            # else: change-point precedes rollout → rollout is not the cause
        else:
            # No change-point — degrade to "rollout in window is suspect"
            # for the timeline display, but DO NOT generate a rollback
            # candidate from this weaker signal (handled below).
            e.minutes_before_incident = None
            deployment_suspected = True

    rollback_candidate: Optional[RollbackCandidate] = None

    # Hard safety gates for rollback candidate generation:
    #   1. We must have seen a suspected deployment.
    #   2. We must have a clean change-point — degraded signal does NOT
    #      generate a rollback candidate.
    #   3. Confidence must clear the threshold.
    #   4. Namespace must not be production.
    can_recommend = (
        deployment_suspected
        and change_point_minutes_ago is not None
        and confidence_score >= threshold
        and not is_production
    )
    if can_recommend:
        # Pick the deployment closest to the change-point.
        suspect_pool = [e for e in recent if e.minutes_before_incident is not None]
        if suspect_pool:
            top = min(suspect_pool, key=lambda e: e.minutes_before_incident or 0.0)
            gap = top.minutes_before_incident if top.minutes_before_incident is not None else 0.0
            rollback_candidate = RollbackCandidate(
                deployment=top.deployment_name,
                current_image=top.image_tag,
                recommended_action="rollback",
                reason=(
                    f"Error spike detected {gap:.1f}m after rollout of "
                    f"{top.image_tag} ({top.deployment_name})"
                ),
                blast_radius_score=blast_radius_score,
                auto_executable=False,
            )
            logger.info({
                "message": "rollback_candidate_generated",
                "service": service,
                "deployment": top.deployment_name,
                "image": top.image_tag,
                "namespace": namespace,
                "confidence_score": confidence_score,
                "blast_radius_score": blast_radius_score,
            })
    elif deployment_suspected:
        # Useful audit line — explains *why* we didn't recommend rollback.
        logger.info({
            "message": "rollback_candidate_suppressed",
            "service": service,
            "namespace": namespace,
            "is_production": is_production,
            "confidence_score": confidence_score,
            "threshold": threshold,
            "had_change_point": change_point_minutes_ago is not None,
        })

    return DeploymentCorrelationResult(
        deployment_timeline=recent,
        deployment_suspected=deployment_suspected,
        rollback_candidate=rollback_candidate,
        source="k8s_api",
        correlation_window_minutes=window,
    )

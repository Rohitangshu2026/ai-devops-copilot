from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict

from app.core.anomaly import compute_anomaly_score
from app.core.blast_radius import BlastRadiusResult, compute_blast_radius
from app.core.audit import record_analysis
from app.core.causality import DEPENDENCY_MAP, validate_causality
from app.core.confidence import score_confidence
from app.core.action_executor import execute_async as action_execute
from app.core.impact import schedule_verification
from app.core.safety import validate as safety_validate
from app.log_processor.classifier import classify_severity
from app.log_processor.extractor import extract_relevant
from app.log_processor.parser import detect_error_type, extract_key_events
from app.log_processor.summarizer import summarize
from app.llm.client import analyze
from app.models.schemas import AnalysisRequest, AnalysisResult, ParsedLog
from app.platforms.registry import get_registry
from app.services.elk_service import fetch_logs
from app.services.memory_store import find_recent_incidents_for_chain, link_incident_to_chain
from app.utils.logger import get_logger

logger = get_logger("agent")

_K8S_EVENTS_AS_EVIDENCE = os.getenv(
    "K8S_EVENTS_AS_EVIDENCE", ""
).strip().lower() in ("1", "true", "yes", "on")


def _heuristic_llm_result(
    *,
    service: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary,
) -> dict:
    """Build a deterministic llm_result-shaped dict when the LLM is unavailable.

    This is the safety net for end-to-end working demos when API keys are
    missing/expired or the model name resolves to 404.  The deterministic
    safety stack downstream of this function is the real authority — the
    LLM only provides the *explanation* and a *suggested* action.  When we
    can't call the LLM, we use the log summary to construct a conservative
    proposal:

    * root_cause: derived from the dominant error pattern in key_events
    * suggestion: "investigate" (no automation hint without the LLM)
    * proposed_action: "notify" — the safest non-destructive action.  The
      safety stack will tighten or downgrade as usual.
    """
    first_event = key_events[0] if key_events else "no events captured"
    if error_type == "dependency_error":
        cause = f"dependency failure suspected from log pattern: {first_event}"
    elif error_type == "runtime_crash":
        cause = f"runtime crash detected: {first_event}"
    elif error_type == "build_failure":
        cause = f"build failure pattern: {first_event}"
    else:
        cause = f"unclassified error pattern: {first_event}"

    return {
        "root_cause": cause,
        "root_causes": [{"cause": cause, "confidence": 0.4}],
        "suggestion": (
            "Manual investigation required.  LLM unavailable — heuristic "
            "fallback proposed 'notify' as the conservative action.  Inspect "
            "logs and k8s events directly."
        ),
        "proposed_action": {
            "type": "notify",
            "target": service,
            "reason": "LLM unavailable; defaulting to notify per heuristic policy",
        },
        "_tool_calls": [],
        "_heuristic_fallback": True,
    }


async def run_analysis(req: AnalysisRequest) -> AnalysisResult:
    _t0 = time.monotonic()

    # ── Resolve platform context (multi-platform refactor) ───────────────────
    # Explicit `req.platform` wins; otherwise reverse-lookup by service;
    # otherwise the registry's built-in "default" fallback applies.  The
    # resolved platform drives: ES index pattern, k8s namespace, dry-run
    # gating, and incident routing metadata.
    _platform = get_registry().resolve(platform=req.platform, service=req.service)
    _namespace = req.namespace or _platform.namespace

    logger.info({
        "message": "analysis_started",
        "service": req.service,
        "env": req.environment,
        "platform": _platform.name,
        "namespace": _namespace,
    })

    raw_logs = await fetch_logs(
        req.service,
        req.environment.value,
        req.lookback_minutes,
        index_pattern=_platform.log_index_pattern,
        service_field=_platform.log_service_field,
        namespace=_namespace,
        pod_name=req.pod_name,
    )

    # Phase 2 — prepend k8s events so the LLM sees the trigger event first.
    # This also ensures k8s-only evidence (no ES logs) bypasses the no-data gate.
    if _K8S_EVENTS_AS_EVIDENCE and req.k8s_events:
        raw_logs = list(req.k8s_events) + raw_logs

    if not raw_logs:
        # No fresh logs is a *finding*, not a server error.  Returning 500
        # makes the webhook chain and the demo brittle — a quiet pod is the
        # most common case in dev/staging.  Build a minimal analysis result
        # tagged so the dashboard and Slack route it as low-priority.
        logger.info({
            "message": "analysis_no_logs",
            "service": req.service,
            "platform": _platform.name,
            "lookback_minutes": req.lookback_minutes,
        })
        return AnalysisResult(
            service=req.service,
            environment=req.environment.value,
            platform=_platform.name,
            namespace=_namespace,
            root_cause="No log activity in window",
            root_causes=[{
                "cause": (
                    f"No documents found for service='{req.service}' in "
                    f"index='{_platform.log_index_pattern}' over the last "
                    f"{req.lookback_minutes}m.  Service may be idle, scaled "
                    f"to zero, or the log shipper is not reaching ES."
                ),
                "confidence": 0.0,
            }],
            suggestion=(
                "Generate traffic against the service or extend "
                "lookback_minutes.  Verify Filebeat is scraping the "
                f"'{_platform.namespace}' namespace."
            ),
            confidence_hint="low",
            confidence_score=0,
            confidence_source="no_data",
            confidence_breakdown=["no logs in window"],
            parsed_log=ParsedLog(
                error_type="unknown",
                severity="low",
                key_events=[],
                summary="no events",
            ),
            raw_evidence=[],
            log_summary={
                "total_events": 0,
                "error_count": 0,
                "warning_count": 0,
                "error_ratio": 0.0,
                "unique_endpoints": [],
                "deduplicated_events": [],
                "time_span_minutes": 0.0,
                "has_only_noise": False,
            },
            proposed_action={
                "type": "no_action",
                "target": req.service,
                "reason": "no log data — nothing to analyze",
            },
            safety_decision="allowed",
            safety_reason="no_action requires no gate",
            action_state="completed",
            has_only_noise=False,
        )

    relevant = extract_relevant(raw_logs)
    # Pass raw_logs so the timeline includes all events (health checks provide
    # time context for error_rate computation). relevant is used for error analysis.
    summary = summarize(raw_logs)

    if summary.has_only_noise:
        logger.warning({
            "message": "only_noise_logs_found",
            "service": req.service,
            "total_events": summary.total_events,
            "hint": (
                "All returned logs are health-check events with no error signals. "
                "This typically means the error events are outside the lookback "
                "window. Run simulate_failure.sh and re-analyze immediately."
            ),
        })

    error_type = detect_error_type(relevant)
    key_events = extract_key_events(relevant)
    severity = classify_severity(relevant, error_type)
    confidence_hint, confidence_score, confidence_breakdown = await score_confidence(
        summary, error_type, severity, service=req.service
    )

    raw_evidence = [str(l.get("message", l)) for l in relevant]

    # ── LLM call with heuristic fallback ─────────────────────────────────────
    # When the LLM is unreachable (bad model name, missing/expired keys,
    # network), we MUST NOT drop the whole incident on the floor — the
    # deterministic safety stack should still gate a reasonable action.
    # The heuristic fallback synthesizes an llm_result-shaped dict from the
    # log summary so downstream code is unchanged.
    _llm_fallback_used = False
    _llm_fallback_reason = ""
    try:
        llm_result = await analyze(
            service=req.service,
            environment=req.environment.value,
            error_type=error_type,
            severity=severity,
            key_events=key_events,
            summary=summary,
            lookback_minutes=req.lookback_minutes,
        )
    except Exception as _llm_exc:  # noqa: BLE001
        import traceback as _tb
        _llm_fallback_used = True
        _llm_fallback_reason = f"{type(_llm_exc).__name__}: {_llm_exc}"
        # Print full traceback to stderr so `kubectl logs` shows the failure
        # path immediately — operators were having to add traceback.print_exc()
        # to routes.py manually otherwise.
        _tb.print_exc()
        logger.warning({
            "message": "llm_unavailable_using_heuristic",
            "service": req.service,
            "error": _llm_fallback_reason,
            "exception_type": type(_llm_exc).__name__,
            "traceback_lines": _tb.format_exc().splitlines()[-5:],
        })
        llm_result = _heuristic_llm_result(
            service=req.service,
            error_type=error_type,
            severity=severity,
            key_events=key_events,
            summary=summary,
        )

    # ranked hypotheses — LLM returns root_causes array
    root_causes: list = llm_result.get("root_causes") or []
    if not root_causes:
        # fallback: wrap legacy root_cause string
        root_causes = [{"cause": llm_result.get("root_cause", ""), "confidence": 0.5}]
    primary_cause = root_causes[0].get("cause", "")

    # causality validation against log evidence
    causality = validate_causality(summary, primary_cause, req.service)
    action_target = causality.action_target or req.service

    if causality.target_redirected:
        logger.info({
            "message": "causality_target_redirected",
            "service": req.service,
            "redirected_to": action_target,
        })

    # ── Phase 10 — Blast-radius estimation ───────────────────────────────────
    _blast: BlastRadiusResult = compute_blast_radius(req.service)
    logger.info({
        "message": "blast_radius_result",
        "service": req.service,
        "score": _blast.score,
        "affected_count": _blast.affected_count,
    })

    # ── Deployment-aware incident correlation ────────────────────────────────
    # Pulls recent rollouts from the k8s API and checks whether the error
    # spike's change-point falls shortly AFTER a rollout.  Surfaces a
    # rollback candidate when confidence is high AND the namespace is
    # non-production.  Never auto-executes — recommendation only.
    _deployment_correlation = None
    try:
        from app.core.deployment_correlation import analyze_deployment_correlation
        _deployment_correlation = analyze_deployment_correlation(
            service=req.service,
            namespace=_namespace,
            incident_summary=summary,
            confidence_score=confidence_score,
            blast_radius_score=_blast.score,
        )
    except Exception as _dep_exc:  # noqa: BLE001
        logger.warning({
            "message": "deployment_correlation_failed",
            "service": req.service,
            "error": str(_dep_exc),
        })

    # ── Phase 9e — Compute statistical anomaly score ─────────────────────────
    # Default -1.0 means "no baseline available" → anomaly gate is skipped.
    # A real z-score (>= 0.0) from an established baseline gates destructive actions.
    _anomaly_score: float = -1.0
    try:
        _anomaly_score = await compute_anomaly_score(req.service, summary)
    except Exception:  # noqa: BLE001
        pass  # gate bypassed when score cannot be computed

    # ── Phase 8e — Pre-safety: determine cascade depth ───────────────────────
    _pre_cascade_depth: int = 0
    _pre_upstream_match: dict | None = None
    try:
        from datetime import datetime, timezone as _tz
        _now_iso = datetime.now(_tz.utc).isoformat()
        _recent = await find_recent_incidents_for_chain(_now_iso, lookback_minutes=10)
        _upstream_services = set(DEPENDENCY_MAP.get(req.service, []))
        for _inc in _recent:
            if _inc.get("service") in _upstream_services:
                _pre_upstream_match = _inc
                _pre_cascade_depth = (_inc.get("cascade_depth") or 0) + 1
                break
    except Exception:  # noqa: BLE001
        pass

    # ── Phase 5 + 9 + 10: safety stack ───────────────────────────────────────
    safety = await safety_validate(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        confidence=confidence_hint,
        proposed_action=llm_result.get("proposed_action", {}),
        causality=causality,
        cascade_depth=_pre_cascade_depth,
        anomaly_score=_anomaly_score,
        blast_radius_score=_blast.score,
    )

    # Override proposed action if safety denied or modified it
    final_action = {**llm_result.get("proposed_action", {}), "type": safety.action}

    # ── Phase 11d — Human approval gate ─────────────────────────────────────
    # Read service criticality from k8s annotation (cached 60s; None in dev)
    _criticality: str | None = None
    try:
        from app.core.blast_radius import service_criticality_from_k8s
        _criticality = service_criticality_from_k8s(req.service)
    except Exception:  # noqa: BLE001
        pass

    approval_request = None
    if safety.allowed and safety.action not in ("notify", "no_action"):
        from app.core.approval import requires_approval, create_approval_request
        _needs_approval, _approval_reason = requires_approval(
            action_type=safety.action,
            service=req.service,
            confidence=confidence_hint,
            blast_radius_score=_blast.score,
            criticality=_criticality,
        )
        if _needs_approval:
            approval_request = create_approval_request(
                incident_id="pending",  # replaced after audit record
                service=req.service,
                action_type=safety.action,
                target=action_target,
                reason=_approval_reason,
            )
            # Send Slack notification (no-op when SLACK_WEBHOOK_URL unset)
            try:
                from app.integrations.slack import notify_approval_required
                from app.utils.config import settings as _settings
                await notify_approval_required(
                    approval_request,
                    confidence_score=confidence_score,
                    confidence_breakdown=confidence_breakdown,
                    blast_radius_score=_blast.score,
                    base_url=f"http://localhost:{_settings.port if hasattr(_settings, 'port') else 8001}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning({"message": "slack_notify_failed", "error": str(exc)})

            logger.info({
                "message": "action_awaiting_approval",
                "service": req.service,
                "action_type": safety.action,
                "approval_id": approval_request.approval_id,
                "reason": _approval_reason,
            })

    # Execute if action is not notify/no_action AND no approval pending.
    # execute_async returns immediately with action_state="executing" so the
    # HTTP response is not held open during the rollout polling loop.
    action_id = str(uuid.uuid4())
    execution_result = None
    if safety.action not in ("notify", "no_action") and approval_request is None:
        execution_result = await action_execute(
            action_id=action_id,
            action_type=safety.action,
            service=action_target,
            dry_run=_platform.is_dry_run(req.environment.value),
        )
        # Schedule impact verification 2 min later (persisted in ES, survives restart)
        await schedule_verification(
            incident_id=action_id,
            service=req.service,
            environment=req.environment.value,
            baseline_error_ratio=summary.error_ratio,
        )

    # Audit log
    _action_state = "awaiting_approval" if approval_request else ("executing" if execution_result else "pending")
    incident_id = await record_analysis(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        confidence_hint=confidence_hint,
        confidence_score=confidence_score,
        causality_verified=causality.verified,
        causality_evidence=causality.matched_evidence,
        root_causes=root_causes,
        proposed_action=final_action,
        safety_decision="allowed" if safety.allowed else "denied",
        safety_reason=safety.reason,
        log_summary=asdict(summary),
        tool_calls=llm_result.pop("_tool_calls", []),
        extra_fields={"action_state": _action_state,
                      "approval_id": approval_request.approval_id if approval_request else None},
    )

    # ── Phase 8e — Temporal incident correlation (link after audit) ──────────
    chain_id: str | None = None
    upstream_incident_id: str | None = None
    cascade_depth: int = 0
    cascade_path: list[str] = []

    try:
        if _pre_upstream_match:
            upstream_id = _pre_upstream_match.get("incident_id", "")
            chain_id = _pre_upstream_match.get("incident_chain_id") or str(uuid.uuid4())
            upstream_incident_id = upstream_id
            cascade_depth = _pre_cascade_depth
            cascade_path = list(_pre_upstream_match.get("cascade_path") or []) + [req.service]
            await link_incident_to_chain(incident_id, chain_id, upstream_id, cascade_depth, cascade_path)
            logger.info({
                "message": "incident_chain_linked",
                "incident_id": incident_id,
                "chain_id": chain_id,
                "upstream_id": upstream_id,
                "cascade_depth": cascade_depth,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "temporal_correlation_failed", "error": str(exc)})

    # Phase 2 — surface pod restart data when k8s evidence was attached
    _pod_status = None
    if req.k8s_events:
        _first_k8s = req.k8s_events[0]
        if _first_k8s.get("restart_count") is not None:
            _pod_status = {
                "pod_name": _first_k8s.get("pod_name"),
                "namespace": _first_k8s.get("namespace"),
                "restart_count": _first_k8s.get("restart_count"),
                "restart_count_delta": _first_k8s.get("restart_count_delta"),
            }

    result = AnalysisResult(
        service=req.service,
        environment=req.environment.value,
        platform=_platform.name,
        namespace=_namespace,
        root_cause=primary_cause,
        root_causes=root_causes,
        suggestion=llm_result.get("suggestion", ""),
        confidence_hint=confidence_hint,
        confidence_score=confidence_score,
        confidence_source=("heuristic_fallback" if _llm_fallback_used else "signal"),
        confidence_breakdown=confidence_breakdown,
        parsed_log=ParsedLog(
            error_type=error_type,
            severity=severity,
            key_events=key_events,
            summary=primary_cause,
        ),
        raw_evidence=raw_evidence[:20],
        log_summary=asdict(summary),
        proposed_action=final_action,
        causality_verified=causality.verified,
        causality_target=action_target if causality.target_redirected else None,
        safety_decision="allowed" if safety.allowed else "denied",
        safety_reason=safety.reason,
        safety_checks=safety.checks,
        incident_id=incident_id,
        execution_result=asdict(execution_result) if execution_result is not None else None,
        incident_chain_id=chain_id,
        upstream_incident_id=upstream_incident_id,
        cascade_depth=cascade_depth,
        cascade_path=cascade_path,
        anomaly_score=_anomaly_score,
        cross_validation=llm_result.pop("_cross_validation", None),
        blast_radius={
            "score": _blast.score,
            "affected_count": _blast.affected_count,
            "affected_services": _blast.affected_services,
            "direct_dependents": _blast.direct_dependents,
            "source": _blast.source,
        },
        approval_id=approval_request.approval_id if approval_request else None,
        action_state=_action_state,
        has_only_noise=summary.has_only_noise,
        # ── Deployment-aware incident correlation ────────────────────
        deployment_timeline=(
            _deployment_correlation.to_response_dict()["deployment_timeline"]
            if _deployment_correlation else []
        ),
        deployment_suspected=(
            _deployment_correlation.deployment_suspected
            if _deployment_correlation else False
        ),
        rollback_candidate=(
            _deployment_correlation.to_response_dict()["rollback_candidate"]
            if _deployment_correlation else None
        ),
        pod_status=_pod_status,
    )

    logger.info({
        "message": "analysis_complete",
        "service": req.service,
        "confidence": confidence_hint,
        "score": confidence_score,
        "error_type": error_type,
        "causality_verified": causality.verified,
        "root_causes_count": len(root_causes),
        "change_point": summary.change_point_description,
        "safety_decision": safety.action,
        "incident_id": incident_id,
    })

    # ── Phase 8c — Prometheus metrics ────────────────────────────────────────
    try:
        from app.utils.prom_metrics import analysis_duration, analysis_total
        outcome_label = result.execution_result.get("status", "unknown") if result.execution_result else "no_action"
        analysis_total.labels(service=req.service, outcome=outcome_label).inc()
        analysis_duration.observe(time.monotonic() - _t0)
    except Exception:  # noqa: BLE001
        pass

    return result

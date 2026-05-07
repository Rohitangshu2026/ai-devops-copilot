"""Blast-radius estimation for proposed actions (Phase 10).

Builds a directed dependency graph from ``devops-copilot/depends-on``
Deployment annotations and computes the transitive set of services
affected by an action on a given target.

The dependency graph is cached for 60 seconds to avoid hammering the
k8s API on every analysis request.  Falls back to the static
``DEPENDENCY_MAP`` from ``causality.py`` when the k8s API is unavailable
(docker-compose mode, missing RBAC, etc.).

Blast-radius scores map to safety-gate tightening:

    critical (>5 affected) — require confidence=high AND severity=critical
    high     (3–5 affected) — require confidence=high AND severity>=high
    medium   (1–2 affected) — require confidence=medium AND severity>=high
    low      (0 affected)   — no additional constraints

These thresholds mirror the service criticality logic (10b) so that
both static operator labels and dynamic graph reachability feed into
the same safety decision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from app.utils.logger import get_logger

logger = get_logger("blast_radius")

_CACHE_TTL = 60.0  # seconds

# ── Cached dependency graph ───────────────────────────────────────────────────

@dataclass
class _DepGraphCache:
    dep_map: dict[str, list[str]]
    fetched_at: float = field(default_factory=time.monotonic)

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < _CACHE_TTL


_graph_cache: _DepGraphCache | None = None


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class BlastRadiusResult:
    """Transitive impact of acting on a service."""

    score: Literal["low", "medium", "high", "critical"]
    affected_services: list[str]
    affected_count: int
    direct_dependents: list[str]
    source: Literal["k8s_annotations", "static_map", "unknown"]


# ── Dependency graph helpers ──────────────────────────────────────────────────

def _score_from_count(count: int) -> Literal["low", "medium", "high", "critical"]:
    if count == 0:
        return "low"
    if count <= 2:
        return "medium"
    if count <= 5:
        return "high"
    return "critical"


def _transitive_dependents(service: str, dep_map: dict[str, list[str]]) -> list[str]:
    """Return all services that directly or transitively depend on *service*.

    ``dep_map`` maps each service to the list of services it *depends on*.
    We invert this to find which services *depend on* the given service.
    """
    # Build reverse map: dependency → set of dependents
    reverse: dict[str, set[str]] = {}
    for svc, deps in dep_map.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(svc)

    # BFS from service through reverse edges
    visited: set[str] = set()
    queue = [service]
    while queue:
        current = queue.pop(0)
        for dependent in reverse.get(current, []):
            if dependent not in visited:
                visited.add(dependent)
                queue.append(dependent)

    return sorted(visited)


def _direct_dependents(service: str, dep_map: dict[str, list[str]]) -> list[str]:
    """Services that directly list *service* in their depends-on annotation."""
    return sorted(svc for svc, deps in dep_map.items() if service in deps)


# ── k8s annotation reader ─────────────────────────────────────────────────────

def _fetch_dep_map_from_k8s() -> dict[str, list[str]] | None:
    """Read devops-copilot/depends-on annotations from all Deployments.

    Returns None when the k8s API is unavailable (graceful degradation).
    """
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()

        apps_v1 = k8s_client.AppsV1Api()
        deps_list = apps_v1.list_deployment_for_all_namespaces(
            label_selector="", _request_timeout=5
        )

        dep_map: dict[str, list[str]] = {}
        for deployment in deps_list.items:
            name = deployment.metadata.name
            annotations = deployment.metadata.annotations or {}
            depends_on_raw = annotations.get("devops-copilot/depends-on", "")
            deps = [d.strip() for d in depends_on_raw.split(",") if d.strip()]
            if deps:
                dep_map[name] = deps

        logger.debug({
            "message": "dep_map_from_k8s",
            "services": list(dep_map.keys()),
        })
        return dep_map

    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "k8s_dep_map_fetch_failed", "error": str(exc)})
        return None


def _get_dep_map() -> tuple[dict[str, list[str]], str]:
    """Return (dep_map, source) using cache + fallback chain."""
    global _graph_cache

    # 1. Cached k8s annotations
    if _graph_cache and _graph_cache.is_fresh():
        return _graph_cache.dep_map, "k8s_annotations"

    # 2. Fresh k8s annotations
    k8s_map = _fetch_dep_map_from_k8s()
    if k8s_map is not None:
        _graph_cache = _DepGraphCache(dep_map=k8s_map)
        return k8s_map, "k8s_annotations"

    # 3. Static fallback from causality.py
    try:
        from app.core.causality import DEPENDENCY_MAP
        return dict(DEPENDENCY_MAP), "static_map"
    except Exception:  # noqa: BLE001
        return {}, "unknown"


# ── Public API ────────────────────────────────────────────────────────────────

def compute_blast_radius(service: str) -> BlastRadiusResult:
    """Compute the blast radius of acting on *service*.

    Returns a :class:`BlastRadiusResult` with transitive affected services
    and a risk score.  The result is suitable for inclusion in
    ``AnalysisResult`` and the safety-gate tightening logic.
    """
    dep_map, source = _get_dep_map()
    transitive = _transitive_dependents(service, dep_map)
    direct = _direct_dependents(service, dep_map)
    count = len(transitive)
    score = _score_from_count(count)

    logger.debug({
        "message": "blast_radius_computed",
        "service": service,
        "score": score,
        "affected_count": count,
        "direct_dependents": direct,
        "source": source,
    })

    return BlastRadiusResult(
        score=score,
        affected_services=transitive,
        affected_count=count,
        direct_dependents=direct,
        source=source,
    )


def service_criticality_from_k8s(service: str) -> str | None:
    """Read the devops-copilot/criticality annotation for *service*.

    Returns the annotation value (``low``, ``medium``, ``high``, ``critical``)
    or None when the annotation is absent or k8s is unreachable.
    """
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()

        apps_v1 = k8s_client.AppsV1Api()
        deployments = apps_v1.list_deployment_for_all_namespaces(
            _request_timeout=5
        )
        for dep in deployments.items:
            if dep.metadata.name == service:
                annotations = dep.metadata.annotations or {}
                return annotations.get("devops-copilot/criticality")
        return None
    except Exception:  # noqa: BLE001
        return None

"""Phase 2 — K8s events as first-class evidence.

Converts a raw WatcherEvent into a dict whose shape is compatible with the
existing log-processing pipeline (extract_relevant / summarize /
detect_error_type all iterate raw_logs and look at ``level`` + ``message``).

The module-level ``_DEFAULT_POD_STATUS_CACHE`` accumulates restart-count
history across dispatches so ``restart_count_delta`` reflects how many new
restarts occurred since the *previous* watcher-triggered analysis for the
same pod — not the pod's total lifetime count.

Feature flag: ``K8S_EVENTS_AS_EVIDENCE`` — this module is always importable;
the flag is checked in ``app.core.agent`` and ``app.watchers.k8s_event_watcher``
before calling ``normalize_event``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger("k8s_evidence")

# ── Restart-count cache ───────────────────────────────────────────────────────
# Maps (namespace, pod_name) → last known total restart_count.
# Insertion-ordered dict (Python 3.7+) gives us O(1) FIFO eviction.

_POD_CACHE_MAX = 256
_DEFAULT_POD_STATUS_CACHE: Dict[Tuple[str, str], int] = {}


def _cache_evict_if_full(
    cache: Dict[Tuple[str, str], int],
    key: Tuple[str, str],
) -> None:
    """Evict the oldest entry when the cache is at capacity and key is new."""
    if len(cache) >= _POD_CACHE_MAX and key not in cache:
        oldest = next(iter(cache))
        del cache[oldest]


# ── Pod status reader ─────────────────────────────────────────────────────────


def read_pod_status(namespace: str, pod_name: str, *, timeout: int = 10) -> dict:
    """Read current pod status from the k8s API.

    Always best-effort — returns ``{}`` on ImportError, RBAC failure,
    transient 5xx, or any other exception.  Callers must handle the empty
    dict case (``restart_count`` stays ``None``).
    """
    try:
        from kubernetes import client as k8s_client  # type: ignore[import]
        from kubernetes import config as k8s_config  # type: ignore[import]
    except ImportError:
        return {}

    try:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            try:
                k8s_config.load_kube_config()
            except Exception:  # noqa: BLE001
                return {}

        v1 = k8s_client.CoreV1Api()
        pod = v1.read_namespaced_pod(
            name=pod_name,
            namespace=namespace,
            _request_timeout=timeout,
        )
        container_statuses = (
            getattr(pod.status, "container_statuses", None) or []
        )
        restart_count = sum(
            int(getattr(cs, "restart_count", 0) or 0)
            for cs in container_statuses
        )
        return {"restart_count": restart_count}
    except Exception:  # noqa: BLE001
        return {}


# ── Event normaliser ──────────────────────────────────────────────────────────


def normalize_event(
    event: Any,
    *,
    pod_status_cache: Optional[Dict[Tuple[str, str], int]] = None,
) -> dict:
    """Convert a ``WatcherEvent`` to a dict compatible with the raw_logs pipeline.

    ``pod_status_cache`` is the (namespace, pod_name) → restart_count memo
    table used for delta computation.  Passing ``None`` (the default) uses the
    module-level ``_DEFAULT_POD_STATUS_CACHE``.  Tests inject their own dict
    for isolation.

    The returned dict is a valid pseudo-log entry:
    - ``level`` / ``@timestamp`` / ``endpoint`` shims let ``extract_relevant``,
      ``summarize``, and ``detect_error_type`` treat it like any other log line.
    - ``type="k8s_event"`` lets downstream code distinguish it from app logs.
    """
    if pod_status_cache is None:
        pod_status_cache = _DEFAULT_POD_STATUS_CACHE

    # Pull all fields through getattr so both dataclasses and SimpleNamespaces work
    reason: str = getattr(event, "reason", "") or ""
    severity: str = getattr(event, "severity", "warning") or "warning"
    message: str = getattr(event, "message", "") or ""
    involved_kind: str = getattr(event, "involved_kind", "") or ""
    involved_name: str = getattr(event, "involved_name", "") or ""
    service: str = getattr(event, "service", "") or ""
    namespace: str = getattr(event, "namespace", "") or ""
    count: int = int(getattr(event, "count", 1) or 1)

    _now_iso = datetime.now(timezone.utc).isoformat()
    first_seen: str = getattr(event, "first_seen", None) or _now_iso
    last_seen: str = getattr(event, "last_seen", None) or _now_iso

    # pod_name is only meaningful when the involved object is a Pod
    pod_name: Optional[str] = involved_name if involved_kind == "Pod" else None

    # Restart-count delta — best-effort, never blocks analysis
    restart_count: Optional[int] = None
    restart_count_delta: Optional[int] = None

    if pod_name and namespace:
        status = read_pod_status(namespace, pod_name)
        if status:
            restart_count = status.get("restart_count")

        if restart_count is not None:
            cache_key: Tuple[str, str] = (namespace, pod_name)
            if cache_key in pod_status_cache:
                prev = pod_status_cache[cache_key]
                restart_count_delta = max(0, restart_count - prev)
            else:
                restart_count_delta = 0  # first sight

            _cache_evict_if_full(pod_status_cache, cache_key)
            pod_status_cache[cache_key] = restart_count

    level = "ERROR" if severity == "critical" else "WARNING"

    return {
        "type": "k8s_event",
        "severity": severity,
        "reason": reason,
        "message": message,
        "involved_kind": involved_kind,
        "involved_name": involved_name,
        "service": service,
        "namespace": namespace,
        "pod_name": pod_name,
        "restart_count": restart_count,
        "restart_count_delta": restart_count_delta,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "count": count,
        # Pipeline compatibility shims
        "level": level,
        "@timestamp": last_seen,
        "endpoint": "",
    }

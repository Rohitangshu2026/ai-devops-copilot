"""Kubernetes event-driven incident trigger — Phase 1.

Watches a single namespace (default ``spyroom``) via ``kubernetes.watch.Watch``
and dispatches the existing analyze pipeline when a high-signal event reason
fires.  **No analysis logic lives here.**  This module only:

1. Runs a synchronous Watch loop in a dedicated thread (kubernetes-python's
   Watch is sync-only).
2. Filters events by REASON (never by ``type`` — some runtimes emit OOMKilled
   as type=Normal).
3. Resolves the involved object to a SpyRoom *service* name.
4. Applies a 5-minute ``(service, reason)`` cooldown dedupe.
5. Hands the event to an asyncio dispatch coroutine through a bounded queue.
6. The dispatch coroutine builds an ``AnalysisRequest`` and calls
   :func:`app.core.agent.run_analysis` — concurrency-capped by a semaphore.

Safeguards baked in:

* **Bounded async queue** (default maxsize=100) drops oldest events on
  overflow with a counter increment.
* **Global sliding-window rate limit** (default 20 dispatches/min) on top of
  the per-(service,reason) cooldown.
* **Concurrency semaphore** (default 3) caps in-flight analyses.
* **Reconnect backoff** (1s → 30s cap) on Watch stream errors.
* **K8s API timeouts** (10s on read calls, 60s on stream timeout) so a
  stuck connection self-recovers.

Phase 2 (`K8S_EVENTS_AS_EVIDENCE`) will extend this module with an event
normaliser; Phase 1 ships only the trigger plumbing.

Feature flag: ``K8S_WATCHER_ENABLED=false`` (default) — rollback is a
1-minute env change + pod restart.

Reuses (do not reimplement):

* Soft-import pattern from :mod:`app.core.blast_radius`,
  :mod:`app.core.deployment_correlation`, :mod:`app.llm.tools`.
* Lifespan task pattern from :mod:`app.main`.
* :func:`app.platforms.registry.get_registry` for service-name validation.
* :func:`app.core.agent.run_analysis` as the dispatch target.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

from app.utils.logger import get_logger
from app.utils.prom_metrics import (
    k8s_analyses_triggered_total,
    k8s_concurrent_analyses,
    k8s_cooldown_hits_total,
    k8s_events_dropped_total,
    k8s_events_received_total,
    k8s_events_skipped_total,
    k8s_queue_depth,
    k8s_watcher_reconnects_total,
)

logger = get_logger("k8s_event_watcher")


# ── Reason allowlists (filter by reason only, NEVER by event.type) ───────────
#
# Some runtimes (containerd vs cri-o, cgroup v1 vs v2) emit OOMKilled and
# Killing as event.type=Normal.  Reason-based gating is the only reliable
# signal across runtimes.

CRITICAL_REASONS = {
    # Pod-level — application can't start or stay up
    "OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "FailedScheduling",
    # Node / infrastructure
    "NodeNotReady", "Evicted",
    "MemoryPressure", "DiskPressure", "PIDPressure",
    "NetworkUnavailable",
}

WARNING_REASONS = {
    "Unhealthy", "BackOff", "Failed",
    "FailedMount", "Killing",
}

IGNORE_REASONS = {
    "Pulled", "Created", "Started", "Scheduled",
    "SuccessfulCreate", "SuccessfulRescale", "SuccessfulDelete",
    "Synced", "Pulling",
}

# Infrastructure subset — used for severity escalation in Phase 4
INFRASTRUCTURE_REASONS = {
    "NodeNotReady", "Evicted",
    "MemoryPressure", "DiskPressure", "PIDPressure",
    "NetworkUnavailable",
}


def severity_for(reason: str) -> str:
    """Map a reason to its severity tier — phase-2 evidence ordering uses this."""
    if reason in CRITICAL_REASONS:
        return "critical"
    if reason in WARNING_REASONS:
        return "warning"
    return "ignore"


# ── Tunables (env-overridable for ops) ───────────────────────────────────────


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


ENABLED = _env_bool("K8S_WATCHER_ENABLED", False)
NAMESPACE = os.getenv("K8S_WATCHER_NAMESPACE", "spyroom").strip()
COOLDOWN_SECONDS = _env_int("K8S_WATCHER_COOLDOWN_SECONDS", 300)
QUEUE_MAXSIZE = _env_int("K8S_WATCHER_QUEUE_MAXSIZE", 100)
MAX_CONCURRENT = _env_int("K8S_WATCHER_MAX_CONCURRENT", 3)
RECONNECT_BACKOFF_MAX = _env_int("K8S_WATCHER_RECONNECT_BACKOFF_MAX", 30)
RATE_LIMIT_PER_MIN = _env_int("K8S_WATCHER_RATE_LIMIT_PER_MIN", 20)
LOOKBACK_MIN = _env_int("K8S_WATCHER_LOOKBACK_MIN", 15)
STREAM_TIMEOUT_SECONDS = _env_int("K8S_WATCHER_STREAM_TIMEOUT_SECONDS", 60)
API_REQUEST_TIMEOUT = _env_int("K8S_WATCHER_API_REQUEST_TIMEOUT", 10)
HEARTBEAT_INTERVAL = _env_int("K8S_WATCHER_HEARTBEAT_INTERVAL", 300)


# ── In-memory state (cleared on pod restart — intentional per plan) ──────────


@dataclass
class _State:
    """All mutable watcher state in one place — easy to reset for tests."""
    cooldowns: Dict[Tuple[str, str], float] = field(default_factory=dict)
    rate_window: Deque[float] = field(default_factory=deque)
    last_event_at: float = 0.0


_state = _State()


def reset_state() -> None:
    """Test helper — wipes cooldowns + rate window + heartbeat timestamp."""
    _state.cooldowns.clear()
    _state.rate_window.clear()
    _state.last_event_at = 0.0


# ── Service-name resolution ──────────────────────────────────────────────────


_POD_HASH_SUFFIX_RE = re.compile(r"-[a-z0-9]+-[a-z0-9]+$")
_RS_HASH_SUFFIX_RE = re.compile(r"-[a-z0-9]+$")


def _strip_pod_suffix(name: str) -> str:
    """``room-service-6c5685cc89-n6clv`` → ``room-service``."""
    return _POD_HASH_SUFFIX_RE.sub("", name)


def _strip_rs_suffix(name: str) -> str:
    """``room-service-6c5685cc89`` → ``room-service``."""
    return _RS_HASH_SUFFIX_RE.sub("", name)


def resolve_service(involved_kind: str, involved_name: str) -> Optional[str]:
    """Resolve ``event.involvedObject`` to a SpyRoom service name, or None.

    Order:
      1. Pod  → strip ``-<rs>-<pod>`` suffix, validate against the registry.
      2. Deployment / StatefulSet / DaemonSet → use name as-is.
      3. ReplicaSet → strip trailing hash.
      4. Anything else (Node, Service, Namespace, …) → None (skip).
    """
    if not involved_name:
        return None
    kind = (involved_kind or "").strip()
    if kind == "Pod":
        candidate = _strip_pod_suffix(involved_name)
    elif kind in ("Deployment", "StatefulSet", "DaemonSet"):
        candidate = involved_name
    elif kind == "ReplicaSet":
        candidate = _strip_rs_suffix(involved_name)
    else:
        return None

    # Best-effort validation against the registered platform; fall back to
    # the candidate name on registry/lookup failure (still a useful trigger).
    try:
        from app.platforms.registry import get_registry
        plat = get_registry().for_service(candidate)
        if plat is not None:
            return candidate
        # Lower-priority: maybe the candidate matches some platform's service
        # name even without precise registry hit — keep the candidate as-is.
        return candidate
    except Exception:  # noqa: BLE001
        return candidate


# ── Cooldown / rate limit ────────────────────────────────────────────────────


def _within_cooldown(service: str, reason: str, now: float) -> bool:
    """True when ``(service, reason)`` fired within the last COOLDOWN_SECONDS."""
    last = _state.cooldowns.get((service, reason))
    return last is not None and (now - last) < COOLDOWN_SECONDS


def _record_dispatch(service: str, reason: str, now: float) -> None:
    """Update cooldown table and sliding rate-limit window."""
    _state.cooldowns[(service, reason)] = now
    _state.rate_window.append(now)


def _rate_limit_exceeded(now: float) -> bool:
    """Check sliding 60-second window against ``RATE_LIMIT_PER_MIN``."""
    horizon = now - 60.0
    # Pop expired entries from the front
    while _state.rate_window and _state.rate_window[0] < horizon:
        _state.rate_window.popleft()
    return len(_state.rate_window) >= RATE_LIMIT_PER_MIN


# ── Event filtering ──────────────────────────────────────────────────────────


@dataclass
class WatcherEvent:
    """The minimal accepted-event payload that crosses the thread boundary."""
    namespace: str
    reason: str
    severity: str
    message: str
    involved_kind: str
    involved_name: str
    service: str
    count: int
    received_at: float
    # Phase 2 — ISO timestamps extracted from the raw k8s event object
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


def classify_event(event_obj) -> Optional[WatcherEvent]:
    """Inspect a raw k8s event object → ``WatcherEvent`` or None.

    Returns None on:
      * unknown reason (skipped with `unknown_reason` log)
      * reason in `IGNORE_REASONS`
      * malformed event (missing ``involvedObject``)
      * unresolvable service (`Node`, `Namespace`, …)
    """
    try:
        reason = (event_obj.reason or "").strip()
    except AttributeError:
        return None

    if not reason:
        return None

    if reason in IGNORE_REASONS:
        k8s_events_skipped_total.labels(
            namespace=NAMESPACE, reason="ignore_set",
        ).inc()
        return None

    severity = severity_for(reason)
    if severity == "ignore":
        # Reason not in any allowlist
        logger.info({
            "message": "k8s_event_skipped",
            "namespace": NAMESPACE,
            "reason": reason,
            "skip_reason": "unknown_reason",
        })
        k8s_events_skipped_total.labels(
            namespace=NAMESPACE, reason="unknown_reason",
        ).inc()
        return None

    involved = getattr(event_obj, "involved_object", None)
    if involved is None:
        return None
    involved_kind = getattr(involved, "kind", "") or ""
    involved_name = getattr(involved, "name", "") or ""

    service = resolve_service(involved_kind, involved_name)
    if not service:
        k8s_events_skipped_total.labels(
            namespace=NAMESPACE, reason="unresolvable_service",
        ).inc()
        return None

    def _iso(dt) -> Optional[str]:
        if dt is None:
            return None
        try:
            return dt.isoformat()
        except Exception:  # noqa: BLE001
            return None

    return WatcherEvent(
        namespace=NAMESPACE,
        reason=reason,
        severity=severity,
        message=(getattr(event_obj, "message", "") or "")[:500],
        involved_kind=involved_kind,
        involved_name=involved_name,
        service=service,
        count=int(getattr(event_obj, "count", 1) or 1),
        received_at=time.monotonic(),
        first_seen=_iso(getattr(event_obj, "first_timestamp", None)),
        last_seen=_iso(getattr(event_obj, "last_timestamp", None)),
    )


# ── Sync watch loop (runs in dedicated thread) ───────────────────────────────


def _build_default_stream_factory(namespace: str):
    """Return a no-arg callable that yields k8s events; or None on unavailability.

    Split out so tests can inject a fake factory without monkeying with
    ``sys.modules['kubernetes']``.  The function returns ``None`` (not a
    callable) when the kubernetes client or kubeconfig is unreachable; the
    caller logs once and exits — never raises.
    """
    try:
        from kubernetes import client as k8s_client  # type: ignore[import]
        from kubernetes import config as k8s_config  # type: ignore[import]
        from kubernetes import watch as k8s_watch    # type: ignore[import]
    except ImportError:
        logger.warning({
            "message": "k8s_event_watcher_unavailable",
            "reason": "kubernetes python package not installed",
        })
        return None

    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning({
                "message": "k8s_event_watcher_unavailable",
                "reason": "no in-cluster or local kubeconfig",
                "error": str(exc),
            })
            return None

    v1 = k8s_client.CoreV1Api()

    def _factory():
        w = k8s_watch.Watch()
        stream = w.stream(
            v1.list_namespaced_event,
            namespace=namespace,
            timeout_seconds=STREAM_TIMEOUT_SECONDS,
            _request_timeout=API_REQUEST_TIMEOUT * 2,
        )
        return stream, w.stop

    return _factory


def _run_watch_loop(
    namespace: str,
    threading_stop: threading.Event,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
    stream_factory=None,    # Optional[Callable[[], (Iterator, Callable)]]
) -> None:
    """Blocking watch loop with exponential reconnect backoff.

    Runs on a thread launched from the asyncio event loop's executor.  Pushes
    accepted events into ``queue`` via ``loop.call_soon_threadsafe``.
    Terminates promptly when ``threading_stop`` is set — never hangs the
    process shutdown.
    """
    if stream_factory is None:
        stream_factory = _build_default_stream_factory(namespace)
        if stream_factory is None:
            return    # logged inside the factory builder

    backoff_seconds = 1.0
    logger.info({"message": "watch_thread_entered", "namespace": namespace})

    while not threading_stop.is_set():
        stop_fn = None
        try:
            stream, stop_fn = stream_factory()
            for event in stream:
                if threading_stop.is_set():
                    break

                _state.last_event_at = time.monotonic()
                raw_event = event.get("object") if isinstance(event, dict) else event
                if raw_event is None:
                    continue

                classified = classify_event(raw_event)
                if classified is None:
                    continue

                k8s_events_received_total.labels(
                    namespace=classified.namespace,
                    reason=classified.reason,
                    severity=classified.severity,
                ).inc()

                # Hand to asyncio queue from this thread.
                loop.call_soon_threadsafe(_enqueue_or_drop, queue, classified)

            # Stream ended normally (server-side timeout) → reset backoff.
            backoff_seconds = 1.0

        except Exception as exc:  # noqa: BLE001
            k8s_watcher_reconnects_total.labels(namespace=namespace).inc()
            logger.warning({
                "message": "watcher_reconnect",
                "namespace": namespace,
                "error": str(exc)[:200],
                "backoff_seconds": round(backoff_seconds, 2),
            })
            # Wait responsive to shutdown
            threading_stop.wait(timeout=backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2.0, float(RECONNECT_BACKOFF_MAX))

        finally:
            # Best-effort stop on the underlying watch — never blocks.
            if stop_fn is not None:
                try:
                    stop_fn()
                except Exception:  # noqa: BLE001
                    pass

    logger.info({"message": "watch_thread_exited", "namespace": namespace})


def _enqueue_or_drop(queue: asyncio.Queue, event: WatcherEvent) -> None:
    """Drop-oldest overflow policy.  Runs on the asyncio loop thread."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # Remove one oldest entry then retry.  Single-retry only — if it still
        # fails, the event is irrecoverably dropped (very unlikely).
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            k8s_events_dropped_total.labels(namespace=event.namespace).inc()
            logger.warning({
                "message": "k8s_event_skipped",
                "skip_reason": "queue_full",
                "namespace": event.namespace,
                "reason": event.reason,
                "service": event.service,
            })
            k8s_events_skipped_total.labels(
                namespace=event.namespace, reason="queue_full",
            ).inc()
            return
    k8s_queue_depth.labels(namespace=event.namespace).set(queue.qsize())


# ── Async dispatch coroutine ─────────────────────────────────────────────────


_DISPATCH_POLL_INTERVAL = 0.2   # seconds — bounded latency for stop_event check


async def _dispatch_loop(
    queue: asyncio.Queue,
    stop_event: asyncio.Event,
    semaphore: asyncio.Semaphore,
) -> None:
    """Pull from the queue, apply cooldown / rate-limit, dispatch.

    Uses ``asyncio.wait_for`` with a short timeout so the loop wakes
    periodically and always observes ``stop_event``.  The 200 ms polling
    overhead is negligible and dramatically simpler than nested wait()
    over two tasks with cancellation handling.
    """
    logger.info({"message": "dispatch_loop_entered"})
    while not stop_event.is_set():
        try:
            event: WatcherEvent = await asyncio.wait_for(
                queue.get(), timeout=_DISPATCH_POLL_INTERVAL,
            )
        except asyncio.TimeoutError:
            continue   # no event in interval → re-check stop_event
        except asyncio.CancelledError:
            return

        k8s_queue_depth.labels(namespace=event.namespace).set(queue.qsize())

        now = time.monotonic()

        if _within_cooldown(event.service, event.reason, now):
            k8s_cooldown_hits_total.labels(
                service=event.service, reason=event.reason,
            ).inc()
            k8s_events_skipped_total.labels(
                namespace=event.namespace, reason="cooldown",
            ).inc()
            logger.info({
                "message": "k8s_event_skipped",
                "skip_reason": "cooldown",
                "namespace": event.namespace,
                "reason": event.reason,
                "service": event.service,
            })
            continue

        if _rate_limit_exceeded(now):
            k8s_events_skipped_total.labels(
                namespace=event.namespace, reason="rate_limit",
            ).inc()
            logger.warning({
                "message": "k8s_event_skipped",
                "skip_reason": "rate_limit",
                "namespace": event.namespace,
                "reason": event.reason,
                "service": event.service,
            })
            continue

        # All gates passed → record + dispatch (concurrency-limited).
        _record_dispatch(event.service, event.reason, now)

        logger.info({
            "message": "k8s_event_detected",
            "namespace": event.namespace,
            "reason": event.reason,
            "severity": event.severity,
            "service": event.service,
            "involved_kind": event.involved_kind,
            "involved_name": event.involved_name,
            "count": event.count,
        })

        asyncio.create_task(_handle_with_semaphore(event, semaphore))

    logger.info({"message": "dispatch_loop_exited"})


async def _handle_with_semaphore(
    event: WatcherEvent, semaphore: asyncio.Semaphore,
) -> None:
    """Wrap ``_run_analysis_for_event`` in the concurrency semaphore."""
    async with semaphore:
        k8s_concurrent_analyses.inc()
        try:
            await _run_analysis_for_event(event)
        finally:
            k8s_concurrent_analyses.dec()


async def _run_analysis_for_event(event: WatcherEvent) -> None:
    """Build an ``AnalysisRequest`` and call the existing analyzer.

    Phase 1: no k8s event payload — the trigger is the only contribution.
    Phase 2 will extend this to attach ``k8s_events=[normalized_event]``.
    """
    k8s_analyses_triggered_total.labels(
        namespace=event.namespace,
        service=event.service,
        reason=event.reason,
    ).inc()
    logger.info({
        "message": "k8s_event_analysis_triggered",
        "namespace": event.namespace,
        "service": event.service,
        "reason": event.reason,
        "severity": event.severity,
    })

    try:
        from app.core.agent import run_analysis
        from app.models.schemas import AnalysisRequest, Environment

        # Phase 2: build normalized k8s event evidence when flag is on
        k8s_events_list = []
        if _env_bool("K8S_EVENTS_AS_EVIDENCE", False):
            from app.services.k8s_evidence import normalize_event
            k8s_events_list = [normalize_event(event)]

        pod_name = event.involved_name if event.involved_kind == "Pod" else None

        req = AnalysisRequest(
            service=event.service,
            environment=Environment.dev,
            lookback_minutes=LOOKBACK_MIN,
            platform=None,             # registry resolves via service
            namespace=event.namespace,
            pod_name=pod_name,
            k8s_events=k8s_events_list,
        )
        result = await run_analysis(req)

        logger.info({
            "message": "k8s_event_analysis_completed",
            "namespace": event.namespace,
            "service": event.service,
            "reason": event.reason,
            "incident_id": getattr(result, "incident_id", None),
            "confidence_source": getattr(result, "confidence_source", None),
            "proposed_action": (
                (result.proposed_action or {}).get("type")
                if getattr(result, "proposed_action", None) else None
            ),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "message": "k8s_event_analysis_failed",
            "namespace": event.namespace,
            "service": event.service,
            "reason": event.reason,
            "error": str(exc)[:200],
        })


# ── Heartbeat (stuck-watcher detector) ───────────────────────────────────────


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    """Log ``watcher_stuck`` when no event has been received in HEARTBEAT_INTERVAL.

    Useful for alerting — a healthy cluster occasionally emits ``Pulled``
    (filtered) which still bumps ``_state.last_event_at`` because we set it
    before classification.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            return

        if _state.last_event_at == 0.0:
            continue  # never seen anything yet — startup grace period

        idle = time.monotonic() - _state.last_event_at
        if idle > HEARTBEAT_INTERVAL:
            logger.warning({
                "message": "watcher_stuck",
                "namespace": NAMESPACE,
                "idle_seconds": round(idle, 1),
                "hint": "no k8s events received for the heartbeat interval",
            })


# ── Public entrypoint (called from app/main.py lifespan) ─────────────────────


async def start_watcher(stop_event: asyncio.Event) -> None:
    """Start the watcher and run until ``stop_event`` is set.

    Hard-wired to disable when ``K8S_WATCHER_ENABLED=false``.  Exits
    cleanly on the stop signal:

      1. Set the thread-local stop flag.
      2. Wait for the executor task to finish (capped at 5s).
      3. Cancel the dispatch + heartbeat coroutines.

    Reuses the asyncio.Event passed by ``app/main.py`` so a single SIGTERM
    propagates everywhere.
    """
    if not ENABLED:
        logger.info({
            "message": "k8s_event_watcher_disabled",
            "hint": "set K8S_WATCHER_ENABLED=true to enable",
        })
        return

    logger.info({
        "message": "k8s_event_watcher_started",
        "namespace": NAMESPACE,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "queue_maxsize": QUEUE_MAXSIZE,
        "max_concurrent": MAX_CONCURRENT,
        "rate_limit_per_min": RATE_LIMIT_PER_MIN,
    })

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    threading_stop = threading.Event()

    # Bridge stop_event → threading_stop
    async def _bridge_stop() -> None:
        await stop_event.wait()
        threading_stop.set()

    bridge_task = asyncio.create_task(_bridge_stop())
    dispatch_task = asyncio.create_task(
        _dispatch_loop(queue, stop_event, semaphore),
    )
    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))

    # Watch loop runs in the default executor.  asyncio.to_thread wraps that
    # cleanly without us having to manage an executor pool.
    watch_future = asyncio.create_task(
        asyncio.to_thread(
            _run_watch_loop,
            NAMESPACE, threading_stop, loop, queue,
        ),
    )

    try:
        await stop_event.wait()
    finally:
        threading_stop.set()
        # Give the watch thread up to 5s to drain & exit
        try:
            await asyncio.wait_for(watch_future, timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            watch_future.cancel()
        for t in (dispatch_task, heartbeat_task, bridge_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        logger.info({
            "message": "k8s_event_watcher_stopped",
            "namespace": NAMESPACE,
        })

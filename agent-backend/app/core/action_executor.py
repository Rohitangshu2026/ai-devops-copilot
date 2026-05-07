"""Kubernetes action executor (Phase 5 + Phase 6f/6h hardening).

Phase 6f — async execution:
  ``execute_async`` returns immediately with ``status="executing"`` and an
  ``action_id``.  The actual rollout polling runs as a background task that
  updates the incident record on completion.  Slow scale-ups no longer time
  out the HTTP caller.

Phase 6h — lease ownership:
  Each background poll loop acquires an ES lease keyed by ``action_id`` and
  renews it every ``lease_renewal_seconds``.  If the lease is stolen (e.g.
  another pod believes the original owner died) the poll loop aborts.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from app.core.policy import get_policy
from app.core.rollback import capture_snapshot
from app.core.rollback import rollback as do_rollback
from app.services.memory_store import (
    release_lease,
    renew_lease,
    try_acquire_lease,
    update_incident,
)
from app.utils.logger import get_logger

logger = get_logger("action_executor")

_KUBECTL_TIMEOUT = 30
_POLL_INTERVAL = 5
_POLL_MAX_SECONDS = 90

_NOOP_ACTIONS = {"notify", "no_action"}


@dataclass
class ExecutionResult:
    """Outcome of an action execution attempt."""

    action_id: str
    action_type: str
    status: Literal[
        "success", "partial", "failed", "dry_run_ok", "skipped",
        "executing",                            # Phase 6f — initial response
        "CRITICAL_INTERVENTION_REQUIRED",       # Phase 6e terminal failure
    ]
    intended: int
    achieved: int
    error: Optional[str]
    rollback_triggered: bool
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# Process-local store for in-flight async tasks so tests can introspect /
# await them.  Not persisted — durability is handled by Memory Store updates.
_BACKGROUND_TASKS: Dict[str, asyncio.Task] = {}


def _kubectl_available() -> bool:
    """Return True when kubectl can be located on PATH."""
    try:
        r = subprocess.run(["kubectl", "version", "--client"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def _run_kubectl(*args: str, timeout: int = _KUBECTL_TIMEOUT) -> tuple[int, str, str]:
    cmd = ["kubectl", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", "kubectl not found"
    except subprocess.TimeoutExpired:
        return -1, "", "kubectl timed out"
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def _build_command(action_type: str, service: str, namespace: str) -> list[str]:
    base = ["-n", namespace]
    if action_type in ("restart_pod", "trigger_retry"):
        return ["rollout", "restart", f"deployment/{service}", *base]
    if action_type == "rollback":
        return ["rollout", "undo", f"deployment/{service}", *base]
    if action_type == "scale_up":
        return ["scale", "deployment", service, "--replicas=3", *base]
    return []


def _get_deployment_replicas(service: str, namespace: str) -> tuple[int, int]:
    rc, out, err = _run_kubectl("get", "deployment", service, "-n", namespace, "-o", "json")
    if rc != 0 or not out:
        return 1, 0
    try:
        spec = json.loads(out)
        desired = spec.get("spec", {}).get("replicas", 1) or 1
        ready = spec.get("status", {}).get("readyReplicas") or 0
        return desired, ready
    except (json.JSONDecodeError, KeyError):
        return 1, 0


async def _poll_ready(action_id: str, service: str, namespace: str) -> tuple[int, int]:
    """Poll readyReplicas while renewing the lease.

    Returns (intended, achieved).  If the lease is stolen mid-poll, returns
    immediately with the last observed counts so the caller can record the
    abort but does not double-execute.
    """
    deadline = time.monotonic() + _POLL_MAX_SECONDS
    renewal_interval = get_policy().global_.lease_renewal_seconds
    last_renew = time.monotonic()
    intended, ready = 1, 0

    while time.monotonic() < deadline:
        intended, ready = _get_deployment_replicas(service, namespace)
        if ready >= intended:
            return intended, ready

        # Renew the lease periodically.  If renewal fails, the lease was
        # stolen — abort to prevent duplicate concurrent execution.
        if time.monotonic() - last_renew >= renewal_interval:
            ttl = get_policy().global_.lease_ttl_seconds
            if not await renew_lease(_lease_id(action_id), ttl_seconds=ttl):
                logger.warning({
                    "message": "lease_lost_during_poll",
                    "action_id": action_id,
                })
                return intended, ready
            last_renew = time.monotonic()

        await asyncio.sleep(_POLL_INTERVAL)

    return _get_deployment_replicas(service, namespace)


def _lease_id(action_id: str) -> str:
    """Lease key for an action execution."""
    return f"action:{action_id}"


# ── Public API ───────────────────────────────────────────────────────────────


async def execute(
    action_id: str,
    action_type: str,
    service: str,
    namespace: str = "default",
    dry_run: bool = False,
) -> ExecutionResult:
    """Synchronous-style execute (kept for backward compatibility).

    Phase 6f introduced ``execute_async`` for non-blocking responses; this
    function still works the same way and is what tests already exercise.
    """
    return await _execute_sync(action_id, action_type, service, namespace, dry_run)


async def execute_async(
    action_id: str,
    action_type: str,
    service: str,
    namespace: str = "default",
    incident_id: Optional[str] = None,
    dry_run: bool = False,
) -> ExecutionResult:
    """Phase 6f — start an action and return immediately.

    Returns ``status="executing"`` on success-to-start so the HTTP caller is
    not blocked on a 90 s rollout poll.  The full result is written to the
    incident record via ``update_incident`` when polling completes.

    For no-op or dry-run paths, falls through to the synchronous executor
    so tests and dev environments behave the same.
    """
    if action_type in _NOOP_ACTIONS or dry_run:
        return await _execute_sync(action_id, action_type, service, namespace, dry_run)

    if not _kubectl_available():
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="skipped",
            intended=0,
            achieved=0,
            error="kubectl not available",
            rollback_triggered=False,
        )

    cmd_args = _build_command(action_type, service, namespace)
    if not cmd_args:
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="failed",
            intended=0,
            achieved=0,
            error=f"unknown action type: {action_type}",
            rollback_triggered=False,
        )

    # Server-side dry-run + apply happen on the foreground call so we can
    # report failures synchronously.  Polling becomes a background task.
    rc, out, err = _run_kubectl(*cmd_args, "--dry-run=server")
    if rc != 0:
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="failed",
            intended=0,
            achieved=0,
            error=f"dry-run failed: {err.strip()}",
            rollback_triggered=False,
        )

    await capture_snapshot(action_id, service, namespace)

    rc, out, err = _run_kubectl(*cmd_args)
    if rc != 0:
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="failed",
            intended=0,
            achieved=0,
            error=f"apply failed: {err.strip()}",
            rollback_triggered=False,
        )

    # Acquire lease then schedule polling as a background task.
    ttl = get_policy().global_.lease_ttl_seconds
    if not await try_acquire_lease(_lease_id(action_id), ttl_seconds=ttl):
        # Another pod beat us; report executing but don't start a 2nd poll.
        logger.info({
            "message": "execution_lease_already_held",
            "action_id": action_id,
        })
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="executing",
            intended=0,
            achieved=0,
            error=None,
            rollback_triggered=False,
        )

    task = asyncio.create_task(
        _background_poll_and_finalize(action_id, action_type, service, namespace, incident_id)
    )
    _BACKGROUND_TASKS[action_id] = task
    task.add_done_callback(lambda _t: _BACKGROUND_TASKS.pop(action_id, None))

    return ExecutionResult(
        action_id=action_id,
        action_type=action_type,
        status="executing",
        intended=0,
        achieved=0,
        error=None,
        rollback_triggered=False,
    )


# ── Internals ────────────────────────────────────────────────────────────────


async def _background_poll_and_finalize(
    action_id: str,
    action_type: str,
    service: str,
    namespace: str,
    incident_id: Optional[str],
) -> None:
    """Run the post-apply poll loop in the background.

    Updates the incident record with the final ExecutionResult so the
    operator can query state via ``GET /api/v1/incidents/{incident_id}``.
    """
    try:
        intended, achieved = await _poll_ready(action_id, service, namespace)

        rollback_triggered = False
        if intended > 0 and (achieved / intended) < 0.5:
            logger.warning({
                "message": "auto_rollback",
                "action_id": action_id,
                "service": service,
                "intended": intended,
                "achieved": achieved,
            })
            rollback_triggered = await do_rollback(action_id, service, namespace)
            if rollback_triggered:
                final_status: str = "partial"
            else:
                # Phase 6e — terminal failure path
                final_status = "CRITICAL_INTERVENTION_REQUIRED"
        elif achieved >= intended:
            final_status = "success"
        else:
            final_status = "partial"

        result = ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status=final_status,  # type: ignore[arg-type]
            intended=intended,
            achieved=achieved,
            error=None,
            rollback_triggered=rollback_triggered,
        )

        if incident_id:
            await update_incident(
                incident_id,
                {
                    "execution_result": asdict(result),
                    "action_state": (
                        "CRITICAL_INTERVENTION_REQUIRED"
                        if final_status == "CRITICAL_INTERVENTION_REQUIRED"
                        else "completed"
                    ),
                },
            )

        logger.info({
            "message": "background_poll_complete",
            "action_id": action_id,
            "status": final_status,
            "intended": intended,
            "achieved": achieved,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "message": "background_poll_error",
            "action_id": action_id,
            "error": str(exc),
        })
        if incident_id:
            await update_incident(
                incident_id,
                {"action_state": "failed", "execution_error": str(exc)},
            )
    finally:
        await release_lease(_lease_id(action_id))


async def _execute_sync(
    action_id: str,
    action_type: str,
    service: str,
    namespace: str,
    dry_run: bool,
) -> ExecutionResult:
    """Original synchronous execute path retained for backwards compatibility."""
    ts = datetime.now(timezone.utc).isoformat()

    if action_type in _NOOP_ACTIONS:
        logger.info({"message": "action_skipped", "action_id": action_id, "action": action_type})
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="skipped",
            intended=0,
            achieved=0,
            error=None,
            rollback_triggered=False,
            timestamp=ts,
        )

    if not _kubectl_available():
        logger.warning({"message": "kubectl_unavailable", "action_id": action_id})
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="skipped",
            intended=0,
            achieved=0,
            error="kubectl not available",
            rollback_triggered=False,
            timestamp=ts,
        )

    cmd_args = _build_command(action_type, service, namespace)
    if not cmd_args:
        logger.warning({"message": "unknown_action_type", "action_id": action_id, "action": action_type})
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="failed",
            intended=0,
            achieved=0,
            error=f"unknown action type: {action_type}",
            rollback_triggered=False,
            timestamp=ts,
        )

    dry_args = [*cmd_args, "--dry-run=server"]
    rc, out, err = _run_kubectl(*dry_args)
    if rc != 0:
        logger.warning({
            "message": "dry_run_failed",
            "action_id": action_id,
            "action": action_type,
            "stderr": err,
        })
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="failed",
            intended=0,
            achieved=0,
            error=f"dry-run failed: {err.strip()}",
            rollback_triggered=False,
            timestamp=ts,
        )

    if dry_run:
        logger.info({"message": "dry_run_ok", "action_id": action_id, "action": action_type})
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="dry_run_ok",
            intended=0,
            achieved=0,
            error=None,
            rollback_triggered=False,
            timestamp=ts,
        )

    await capture_snapshot(action_id, service, namespace)

    rc, out, err = _run_kubectl(*cmd_args)
    if rc != 0:
        logger.warning({
            "message": "action_apply_failed",
            "action_id": action_id,
            "action": action_type,
            "stderr": err,
        })
        return ExecutionResult(
            action_id=action_id,
            action_type=action_type,
            status="failed",
            intended=0,
            achieved=0,
            error=f"apply failed: {err.strip()}",
            rollback_triggered=False,
            timestamp=ts,
        )

    logger.info({"message": "action_applied", "action_id": action_id, "action": action_type})

    intended, achieved = await _poll_ready(action_id, service, namespace)

    rollback_triggered = False
    if intended > 0 and (achieved / intended) < 0.5:
        logger.warning({
            "message": "auto_rollback",
            "action_id": action_id,
            "service": service,
            "intended": intended,
            "achieved": achieved,
        })
        rollback_triggered = await do_rollback(action_id, service, namespace)
        if rollback_triggered:
            status = "partial"
        else:
            status = "CRITICAL_INTERVENTION_REQUIRED"
    elif achieved >= intended:
        status = "success"
    else:
        status = "partial"

    logger.info({
        "message": "action_complete",
        "action_id": action_id,
        "action": action_type,
        "status": status,
        "intended": intended,
        "achieved": achieved,
        "rollback_triggered": rollback_triggered,
    })

    return ExecutionResult(
        action_id=action_id,
        action_type=action_type,
        status=status,  # type: ignore[arg-type]
        intended=intended,
        achieved=achieved,
        error=None,
        rollback_triggered=rollback_triggered,
        timestamp=ts,
    )

"""Kubernetes action executor for Phase 5.

Applies LLM-approved actions to Kubernetes via kubectl, with:
- Server-side dry-run validation before any real change.
- Rollback snapshot captured before applying.
- Post-apply polling to confirm readyReplicas converges.
- Auto-rollback when fewer than 50 % of replicas become ready.

All operations are safe to call even when kubectl is not installed — the
executor returns a ``skipped`` status rather than raising.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from app.core.rollback import capture_snapshot
from app.core.rollback import rollback as do_rollback
from app.utils.logger import get_logger

logger = get_logger("action_executor")

_KUBECTL_TIMEOUT = 30
_POLL_INTERVAL = 5
_POLL_MAX_SECONDS = 90

# Actions that require no kubectl command.
_NOOP_ACTIONS = {"notify", "no_action"}


@dataclass
class ExecutionResult:
    """Outcome of an action execution attempt."""

    action_id: str
    action_type: str
    status: Literal["success", "partial", "failed", "dry_run_ok", "skipped"]
    intended: int
    achieved: int
    error: Optional[str]
    rollback_triggered: bool
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _kubectl_available() -> bool:
    """Return True when kubectl can be located on PATH."""
    try:
        r = subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def _run_kubectl(*args: str, timeout: int = _KUBECTL_TIMEOUT) -> tuple[int, str, str]:
    """Run kubectl and return (returncode, stdout, stderr)."""
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
    """Return the kubectl arguments for *action_type*."""
    base = ["-n", namespace]
    if action_type in ("restart_pod", "trigger_retry"):
        return ["rollout", "restart", f"deployment/{service}", *base]
    if action_type == "rollback":
        return ["rollout", "undo", f"deployment/{service}", *base]
    if action_type == "scale_up":
        return ["scale", "deployment", service, "--replicas=3", *base]
    return []


def _get_deployment_replicas(service: str, namespace: str) -> tuple[int, int]:
    """Return (desired_replicas, ready_replicas) or (1, 0) on failure."""
    rc, out, err = _run_kubectl("get", "deployment", service, "-n", namespace, "-o", "json")
    if rc != 0 or not out:
        return 1, 0
    try:
        spec = json.loads(out)
        desired = spec.get("spec", {}).get("replicas", 1) or 1
        status = spec.get("status", {})
        ready = status.get("readyReplicas") or 0
        return desired, ready
    except (json.JSONDecodeError, KeyError):
        return 1, 0


async def _poll_ready(service: str, namespace: str) -> tuple[int, int]:
    """Poll until readyReplicas >= replicas or timeout.

    Returns (intended, achieved).
    """
    deadline = time.monotonic() + _POLL_MAX_SECONDS
    while time.monotonic() < deadline:
        intended, ready = _get_deployment_replicas(service, namespace)
        if ready >= intended:
            return intended, ready
        await asyncio.sleep(_POLL_INTERVAL)
    # Final read
    return _get_deployment_replicas(service, namespace)


async def execute(
    action_id: str,
    action_type: str,
    service: str,
    namespace: str = "default",
    dry_run: bool = False,
) -> ExecutionResult:
    """Execute *action_type* against the *service* deployment.

    Flow:
    1. No-op actions (notify, no_action) → skipped immediately.
    2. kubectl unavailable → skipped.
    3. Server-side dry-run → failed on error.
    4. If dry_run=True → return dry_run_ok.
    5. Capture snapshot.
    6. Apply real command.
    7. Poll readyReplicas.
    8. Auto-rollback if achieved/intended < 0.5.
    """
    ts = datetime.now(timezone.utc).isoformat()

    # ── 1. No-op ─────────────────────────────────────────────────────────────
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

    # ── 2. kubectl availability ───────────────────────────────────────────────
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

    # ── 3. Server-side dry-run ───────────────────────────────────────────────
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

    # ── 4. Return dry_run_ok without applying ────────────────────────────────
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

    # ── 5. Snapshot ──────────────────────────────────────────────────────────
    await capture_snapshot(action_id, service, namespace)

    # ── 6. Apply ─────────────────────────────────────────────────────────────
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

    # ── 7. Poll ──────────────────────────────────────────────────────────────
    intended, achieved = await _poll_ready(service, namespace)

    # ── 8. Auto-rollback ─────────────────────────────────────────────────────
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
        status: Literal["success", "partial", "failed", "dry_run_ok", "skipped"] = "partial"
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
        status=status,
        intended=intended,
        achieved=achieved,
        error=None,
        rollback_triggered=rollback_triggered,
        timestamp=ts,
    )

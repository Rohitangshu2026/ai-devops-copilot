"""Kubernetes deployment snapshot and rollback support (Phase 6e hardening).

Captures a lightweight snapshot of a deployment's current state before any
action is taken, and restores it via ``kubectl apply -f -`` on the captured
spec rather than ``kubectl rollout undo`` (which ignores the snapshot and
silently uses the previous revision — broken when both revisions are bad).

Terminal-failure path: if the rollback itself fails to bring the deployment
back to ``Running`` within 90 s, the system enters
``CRITICAL_INTERVENTION_REQUIRED`` and freezes the service indefinitely.

Snapshots are persisted in-memory by ``action_id`` for the life of the
process and additionally serialized into the rollback entry so that an
``apply``-based rollback can re-pin image and replicas exactly.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger("rollback")


@dataclass
class RollbackEntry:
    """Snapshot of a deployment captured before an action."""

    action_id: str
    service: str
    previous_image: str
    previous_replicas: int
    spec_hash: str           # sha256[:16] of deployment JSON
    captured_at: str
    namespace: str = "default"
    raw_spec: Optional[Dict[str, Any]] = field(default=None, repr=False)


# In-memory snapshot store keyed by action_id.  This is sufficient for the
# Phase 6 single-replica-with-restart hardening; Phase 10 will move it into
# Memory Store for HPA multi-replica safety.
_SNAPSHOTS: Dict[str, RollbackEntry] = {}

_ROLLBACK_POLL_INTERVAL = 3
_ROLLBACK_POLL_TIMEOUT = 90


def _run_kubectl(*args: str, stdin: Optional[str] = None, timeout: int = 30) -> Optional[str]:
    """Run a kubectl command and return its stdout, or None on failure."""
    cmd = ["kubectl", *args]
    try:
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning({
                "message": "kubectl_nonzero",
                "cmd": " ".join(cmd),
                "stderr": result.stderr.strip()[:500],
                "returncode": result.returncode,
            })
            return None
        return result.stdout
    except FileNotFoundError:
        logger.warning({"message": "kubectl_not_found", "cmd": " ".join(cmd)})
        return None
    except subprocess.TimeoutExpired:
        logger.warning({"message": "kubectl_timeout", "cmd": " ".join(cmd)})
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "kubectl_error", "cmd": " ".join(cmd), "error": str(exc)})
        return None


async def capture_snapshot(
    action_id: str,
    service: str,
    namespace: str = "default",
) -> Optional[RollbackEntry]:
    """Capture the current deployment spec and store it under *action_id*."""
    output = _run_kubectl(
        "get", "deployment", service,
        "-n", namespace,
        "-o", "json",
    )
    if output is None:
        return None

    try:
        spec = json.loads(output)
    except json.JSONDecodeError as exc:
        logger.warning({"message": "snapshot_json_error", "service": service, "error": str(exc)})
        return None

    spec_hash = hashlib.sha256(output.encode()).hexdigest()[:16]

    containers = (
        spec.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    image = containers[0].get("image", "unknown") if containers else "unknown"
    replicas = spec.get("spec", {}).get("replicas", 1)

    entry = RollbackEntry(
        action_id=action_id,
        service=service,
        previous_image=image,
        previous_replicas=replicas,
        spec_hash=spec_hash,
        captured_at=datetime.now(timezone.utc).isoformat(),
        namespace=namespace,
        raw_spec=spec,
    )
    _SNAPSHOTS[action_id] = entry
    logger.info({
        "message": "snapshot_captured",
        "action_id": action_id,
        "service": service,
        "image": image,
        "replicas": replicas,
        "spec_hash": spec_hash,
    })
    return entry


def get_snapshot(action_id: str) -> Optional[RollbackEntry]:
    """Return the in-memory snapshot for *action_id* if any."""
    return _SNAPSHOTS.get(action_id)


def _strip_runtime_fields(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the parts of the deployment spec that ES/k8s assigns at runtime."""
    cleaned = json.loads(json.dumps(spec))   # deep copy
    cleaned.pop("status", None)
    metadata = cleaned.get("metadata", {}) or {}
    for k in ("resourceVersion", "uid", "generation", "managedFields", "creationTimestamp"):
        metadata.pop(k, None)
    annotations = metadata.get("annotations", {}) or {}
    annotations.pop("deployment.kubernetes.io/revision", None)
    return cleaned


def _get_deployment_status(service: str, namespace: str) -> Optional[Dict[str, Any]]:
    """Return the deployment status block or None."""
    out = _run_kubectl("get", "deployment", service, "-n", namespace, "-o", "json")
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


async def _wait_for_running(service: str, namespace: str, timeout: int = _ROLLBACK_POLL_TIMEOUT) -> bool:
    """Poll until ``readyReplicas >= replicas`` or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        spec = _get_deployment_status(service, namespace)
        if spec:
            desired = spec.get("spec", {}).get("replicas", 1) or 1
            ready = spec.get("status", {}).get("readyReplicas") or 0
            if ready >= desired:
                return True
        await asyncio.sleep(_ROLLBACK_POLL_INTERVAL)
    return False


async def rollback(
    action_id: str,
    service: str,
    namespace: str = "default",
) -> bool:
    """Restore *service* from the snapshot captured for *action_id*.

    Phase 6e: prefer ``kubectl apply -f <snapshot-json>`` so we re-pin the
    exact image and replica count we recorded.  Falls back to
    ``kubectl rollout undo`` only if no snapshot is available.

    Returns True on success, False on hard failure.  The caller is
    responsible for recording the terminal-failure state when False is
    returned.
    """
    entry = _SNAPSHOTS.get(action_id)

    # ── Path 1: snapshot-based rollback (preferred) ─────────────────────────
    if entry is not None and entry.raw_spec is not None:
        cleaned_spec = _strip_runtime_fields(entry.raw_spec)
        spec_json = json.dumps(cleaned_spec)
        out = _run_kubectl("apply", "-f", "-", "-n", namespace, stdin=spec_json)
        if out is None:
            logger.warning({
                "message": "rollback_apply_failed",
                "action_id": action_id,
                "service": service,
            })
            return False

        logger.info({
            "message": "rollback_applied_from_snapshot",
            "action_id": action_id,
            "service": service,
            "image": entry.previous_image,
            "replicas": entry.previous_replicas,
        })

        if not await _wait_for_running(service, namespace):
            logger.warning({
                "message": "rollback_pod_did_not_become_ready",
                "action_id": action_id,
                "service": service,
            })
            return False
        return True

    # ── Path 2: legacy rollout undo when no snapshot exists ─────────────────
    out = _run_kubectl("rollout", "undo", f"deployment/{service}", "-n", namespace)
    if out is None:
        logger.warning({
            "message": "rollback_failed",
            "action_id": action_id,
            "service": service,
        })
        return False

    if not await _wait_for_running(service, namespace):
        logger.warning({
            "message": "rollback_pod_did_not_become_ready",
            "action_id": action_id,
            "service": service,
        })
        return False

    logger.info({
        "message": "rollback_triggered",
        "action_id": action_id,
        "service": service,
        "method": "rollout_undo",
        "output": out.strip()[:200],
    })
    return True


# Test helpers ----------------------------------------------------------------


def _clear_snapshots() -> None:
    """Drop all in-memory snapshots (used by tests)."""
    _SNAPSHOTS.clear()

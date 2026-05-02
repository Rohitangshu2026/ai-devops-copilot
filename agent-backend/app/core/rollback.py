"""Kubernetes deployment snapshot and rollback support.

Captures a lightweight snapshot of a deployment's current state before any
action is taken, and can restore it with ``kubectl rollout undo``.

All functions swallow exceptions and return ``None``/``False`` so a missing
or inaccessible kubectl never blocks the analysis pipeline.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger("rollback")


@dataclass
class RollbackEntry:
    """Snapshot of a deployment captured before an action."""

    action_id: str
    service: str
    previous_image: str
    previous_replicas: int
    spec_hash: str          # sha256[:16] of deployment JSON
    captured_at: str
    namespace: str = "default"


def _run_kubectl(*args: str, timeout: int = 15) -> Optional[str]:
    """Run a kubectl command and return its stdout.

    Returns ``None`` when kubectl is unavailable or exits non-zero.
    """
    cmd = ["kubectl", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning({
                "message": "kubectl_nonzero",
                "cmd": " ".join(cmd),
                "stderr": result.stderr.strip(),
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
    """Capture the current deployment spec and return a RollbackEntry.

    Returns ``None`` when kubectl is unavailable or the deployment cannot
    be fetched.
    """
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

    # Extract image from the first container
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
    )
    logger.info({
        "message": "snapshot_captured",
        "action_id": action_id,
        "service": service,
        "image": image,
        "replicas": replicas,
        "spec_hash": spec_hash,
    })
    return entry


async def rollback(
    action_id: str,
    service: str,
    namespace: str = "default",
) -> bool:
    """Trigger a ``kubectl rollout undo`` for *service*.

    Returns ``True`` on success, ``False`` on any failure.
    """
    output = _run_kubectl("rollout", "undo", f"deployment/{service}", "-n", namespace)
    if output is None:
        logger.warning({
            "message": "rollback_failed",
            "action_id": action_id,
            "service": service,
        })
        return False

    logger.info({
        "message": "rollback_triggered",
        "action_id": action_id,
        "service": service,
        "output": output.strip(),
    })
    return True

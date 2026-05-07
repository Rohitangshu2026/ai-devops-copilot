"""Audit logger for the safety stack.

Every call to ``record_analysis`` persists a structured incident document to
Elasticsearch via ``memory_store.save_incident``.  It never raises — failures
are logged as warnings and the generated incident_id is returned regardless.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.memory_store import save_incident
from app.utils.logger import get_logger

logger = get_logger("audit")


async def record_analysis(
    *,
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    confidence_hint: str,
    confidence_score: int,
    causality_verified: bool,
    causality_evidence: list[str],
    root_causes: list[dict[str, Any]],
    proposed_action: dict[str, Any],
    safety_decision: str,
    safety_reason: str,
    log_summary: dict[str, Any],
    tool_calls: list[dict[str, Any]] | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> str:
    """Build a structured incident document and persist it.

    Returns the ``incident_id`` (a UUID string) even when persistence fails.
    """
    incident_id = str(uuid.uuid4())
    incident: dict[str, Any] = {
        "incident_id": incident_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "environment": environment,
        "error_type": error_type,
        "severity": severity,
        "confidence_hint": confidence_hint,
        "confidence_score": confidence_score,
        "causality_verified": causality_verified,
        "causality_evidence": causality_evidence,
        "root_causes": root_causes,
        "proposed_action": proposed_action,
        "safety_decision": safety_decision,
        "safety_reason": safety_reason,
        "log_summary": log_summary,
        "tool_calls": tool_calls or [],
        "action_state": "pending",
        "outcome": "unknown",
    }

    # Merge caller-supplied extra fields (e.g. approval_id, action_state override)
    if extra_fields:
        incident.update({k: v for k, v in extra_fields.items() if v is not None})

    try:
        await save_incident(incident)
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "record_analysis_failed", "incident_id": incident_id, "error": str(exc)})

    return incident_id

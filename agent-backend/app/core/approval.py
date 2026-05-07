"""Human approval workflow for high-impact actions (Phase 11).

When a proposed action meets any of the criteria below, it is routed
through an approval gate before execution:

  - Service criticality=critical (from k8s annotation)
  - Action type is ``rollback``
  - Blast-radius score is ``high`` or ``critical``
  - Confidence < ``high`` AND action is destructive

An ``ApprovalRequest`` is created with a signed HMAC-SHA256 token.
The token is sent to operators (via Slack if configured) along with
two links: approve and reject.  The agent-backend exposes:

  POST /api/v1/approvals/{approval_id}/approve
  POST /api/v1/approvals/{approval_id}/reject

Both endpoints verify the signed token before acting.  Expired tokens
(> APPROVAL_EXPIRY_SECONDS) return 403.  Forged tokens are also rejected.

State machine:
  pending → awaiting_approval → approved → executing → completed
                              → rejected  (terminal)
                              → approval_expired  (terminal)

For non-approval-required actions (the majority in dev):
  pending → executing → completed  (original fast path, unchanged)
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("approval")

_DESTRUCTIVE_ACTIONS = {"restart_pod", "rollback", "scale_up"}


@dataclass
class ApprovalRequest:
    """An approval request tied to a specific incident and proposed action."""

    approval_id: str
    incident_id: str
    service: str
    action_type: str
    target: str
    reason: str                # why approval is required
    expires_at: float          # Unix timestamp
    signed_token: str          # HMAC-SHA256 proof of authenticity
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "incident_id": self.incident_id,
            "service": self.service,
            "action_type": self.action_type,
            "target": self.target,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


# ── In-memory store (sufficient for single-pod demo; replace with ES for HA) ─

_pending: dict[str, ApprovalRequest] = {}


def _sign(approval_id: str, incident_id: str, expires_at: float) -> str:
    """Generate an HMAC-SHA256 token binding the approval_id to the incident."""
    secret = settings.approval_secret_key.encode()
    message = f"{approval_id}:{incident_id}:{expires_at:.3f}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def create_approval_request(
    incident_id: str,
    service: str,
    action_type: str,
    target: str,
    reason: str,
) -> ApprovalRequest:
    """Create, store, and return a new ApprovalRequest."""
    approval_id = str(uuid.uuid4())
    expires_at = time.time() + settings.approval_expiry_seconds
    signed_token = _sign(approval_id, incident_id, expires_at)

    req = ApprovalRequest(
        approval_id=approval_id,
        incident_id=incident_id,
        service=service,
        action_type=action_type,
        target=target,
        reason=reason,
        expires_at=expires_at,
        signed_token=signed_token,
    )
    _pending[approval_id] = req
    logger.info({
        "message": "approval_request_created",
        "approval_id": approval_id,
        "incident_id": incident_id,
        "service": service,
        "action_type": action_type,
        "expires_in_seconds": settings.approval_expiry_seconds,
    })
    return req


def verify_token(approval_id: str, token: str) -> tuple[bool, str]:
    """Verify a signed token.

    Returns (valid: bool, reason: str).
    Reasons: "ok", "not_found", "expired", "invalid_token"
    """
    req = _pending.get(approval_id)
    if req is None:
        return False, "not_found"
    if req.is_expired():
        _pending.pop(approval_id, None)
        return False, "expired"
    expected = _sign(approval_id, req.incident_id, req.expires_at)
    if not hmac.compare_digest(expected, token):
        return False, "invalid_token"
    return True, "ok"


def approve(approval_id: str, token: str, approved_by: str = "operator") -> dict[str, Any]:
    """Approve an action.  Returns a result dict or raises ValueError."""
    valid, reason = verify_token(approval_id, token)
    if not valid:
        raise ValueError(f"Approval denied: {reason}")

    req = _pending.pop(approval_id)
    logger.info({
        "message": "approval_granted",
        "approval_id": approval_id,
        "incident_id": req.incident_id,
        "approved_by": approved_by,
    })
    return {
        "approved": True,
        "approval_id": approval_id,
        "incident_id": req.incident_id,
        "approved_by": approved_by,
        "action_type": req.action_type,
    }


def reject(approval_id: str, token: str, rejected_by: str = "operator") -> dict[str, Any]:
    """Reject an action.  Returns a result dict or raises ValueError."""
    valid, reason = verify_token(approval_id, token)
    if not valid:
        raise ValueError(f"Rejection denied: {reason}")

    req = _pending.pop(approval_id)
    logger.info({
        "message": "approval_rejected",
        "approval_id": approval_id,
        "incident_id": req.incident_id,
        "rejected_by": rejected_by,
    })
    return {
        "approved": False,
        "approval_id": approval_id,
        "incident_id": req.incident_id,
        "rejected_by": rejected_by,
    }


def get_pending(approval_id: str) -> ApprovalRequest | None:
    """Return the pending request or None if absent/expired."""
    req = _pending.get(approval_id)
    if req and req.is_expired():
        _pending.pop(approval_id, None)
        return None
    return req


# ── Decision: does this action require approval? ─────────────────────────────

def requires_approval(
    action_type: str,
    service: str,
    confidence: str,
    blast_radius_score: str = "low",
    criticality: str | None = None,
) -> tuple[bool, str]:
    """Return (needs_approval: bool, reason: str).

    Approval is required when ANY of:
    - Service is annotated criticality=critical
    - Action type is rollback
    - Blast-radius is high or critical
    - Confidence < high AND action is destructive
    """
    if action_type not in _DESTRUCTIVE_ACTIONS:
        return False, ""

    if criticality == "critical":
        return True, f"service {service} is criticality=critical"

    if action_type == "rollback":
        return True, "rollback actions always require human approval"

    if blast_radius_score in ("high", "critical"):
        return True, f"blast-radius={blast_radius_score} — action affects multiple services"

    if confidence != "high" and action_type in _DESTRUCTIVE_ACTIONS:
        return True, f"destructive action with confidence={confidence} requires approval"

    return False, ""

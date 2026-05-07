"""Tests for Phase 11d — human approval workflow (app/core/approval.py)."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.core.approval import (
    ApprovalRequest,
    approve,
    create_approval_request,
    get_pending,
    reject,
    requires_approval,
    verify_token,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(**kwargs):
    """Create a fresh ApprovalRequest with sensible defaults."""
    defaults = {
        "incident_id": "inc-001",
        "service": "sample-app",
        "action_type": "restart_pod",
        "target": "sample-app",
        "reason": "test reason",
    }
    defaults.update(kwargs)
    return create_approval_request(**defaults)


def _clear_pending():
    """Clear the in-memory pending store between tests."""
    from app.core.approval import _pending
    _pending.clear()


# ---------------------------------------------------------------------------
# Token signing and verification
# ---------------------------------------------------------------------------


def test_create_request_returns_approval_request():
    """create_approval_request returns an ApprovalRequest with all fields."""
    _clear_pending()
    req = _make_request()
    assert req.approval_id
    assert req.incident_id == "inc-001"
    assert req.service == "sample-app"
    assert req.action_type == "restart_pod"
    assert req.signed_token
    assert req.expires_at > time.time()


def test_signed_token_is_consistent():
    """Two calls to _sign with same inputs return the same token."""
    from app.core.approval import _sign
    t1 = _sign("approval-1", "incident-1", 12345.678)
    t2 = _sign("approval-1", "incident-1", 12345.678)
    assert t1 == t2


def test_signed_token_differs_on_different_ids():
    """Different approval_ids produce different tokens (non-trivial signing)."""
    from app.core.approval import _sign
    t1 = _sign("approval-AAA", "incident-1", 12345.678)
    t2 = _sign("approval-BBB", "incident-1", 12345.678)
    assert t1 != t2


def test_verify_token_ok():
    """Valid token verifies as (True, 'ok')."""
    _clear_pending()
    req = _make_request()
    valid, reason = verify_token(req.approval_id, req.signed_token)
    assert valid is True
    assert reason == "ok"


def test_verify_token_not_found():
    """Unknown approval_id returns (False, 'not_found')."""
    _clear_pending()
    valid, reason = verify_token("nonexistent-id", "any-token")
    assert valid is False
    assert reason == "not_found"


def test_verify_token_invalid():
    """Tampered/forged token returns (False, 'invalid_token')."""
    _clear_pending()
    req = _make_request()
    valid, reason = verify_token(req.approval_id, "forged-token-abc123")
    assert valid is False
    assert reason == "invalid_token"


def test_verify_token_expired():
    """Expired token returns (False, 'expired')."""
    _clear_pending()
    with patch("app.core.approval.time.time", return_value=time.time() - 1000):
        req = _make_request()
    valid, reason = verify_token(req.approval_id, req.signed_token)
    assert valid is False
    assert reason in ("expired", "invalid_token")  # expired detected at verify time


def test_verify_removes_expired_from_pending():
    """Expired approval is removed from _pending store after verify."""
    _clear_pending()
    from app.core.approval import _pending
    with patch("app.core.approval.time.time", return_value=time.time() - 1000):
        req = _make_request()
    assert req.approval_id in _pending
    verify_token(req.approval_id, req.signed_token)
    assert req.approval_id not in _pending


# ---------------------------------------------------------------------------
# approve() / reject()
# ---------------------------------------------------------------------------


def test_approve_success():
    """approve() returns result dict with approved=True."""
    _clear_pending()
    req = _make_request()
    result = approve(req.approval_id, req.signed_token, approved_by="alice")
    assert result["approved"] is True
    assert result["approval_id"] == req.approval_id
    assert result["approved_by"] == "alice"
    assert result["incident_id"] == "inc-001"
    assert result["action_type"] == "restart_pod"


def test_approve_removes_from_pending():
    """After approval, request is removed from pending store."""
    _clear_pending()
    from app.core.approval import _pending
    req = _make_request()
    assert req.approval_id in _pending
    approve(req.approval_id, req.signed_token)
    assert req.approval_id not in _pending


def test_approve_raises_on_invalid_token():
    """approve() raises ValueError when token is forged."""
    _clear_pending()
    req = _make_request()
    with pytest.raises(ValueError, match="Approval denied"):
        approve(req.approval_id, "invalid-token")


def test_reject_success():
    """reject() returns result dict with approved=False."""
    _clear_pending()
    req = _make_request()
    result = reject(req.approval_id, req.signed_token, rejected_by="bob")
    assert result["approved"] is False
    assert result["approval_id"] == req.approval_id
    assert result["rejected_by"] == "bob"
    assert result["incident_id"] == "inc-001"


def test_reject_removes_from_pending():
    """After rejection, request is removed from pending store."""
    _clear_pending()
    from app.core.approval import _pending
    req = _make_request()
    assert req.approval_id in _pending
    reject(req.approval_id, req.signed_token)
    assert req.approval_id not in _pending


def test_reject_raises_on_invalid_token():
    """reject() raises ValueError when token is forged."""
    _clear_pending()
    req = _make_request()
    with pytest.raises(ValueError, match="Rejection denied"):
        reject(req.approval_id, "bad-token")


def test_replay_attack_blocked():
    """Re-using an approved token after approval is rejected (not_found)."""
    _clear_pending()
    req = _make_request()
    token = req.signed_token
    # First approval succeeds
    approve(req.approval_id, token)
    # Replay attempt fails (approval_id removed from pending)
    with pytest.raises(ValueError):
        approve(req.approval_id, token)


# ---------------------------------------------------------------------------
# get_pending()
# ---------------------------------------------------------------------------


def test_get_pending_returns_request():
    """get_pending returns the approval request when it exists."""
    _clear_pending()
    req = _make_request()
    found = get_pending(req.approval_id)
    assert found is not None
    assert found.approval_id == req.approval_id


def test_get_pending_returns_none_for_unknown():
    """get_pending returns None for unknown approval_id."""
    _clear_pending()
    assert get_pending("unknown-id") is None


def test_get_pending_returns_none_for_expired():
    """get_pending returns None and removes expired requests."""
    _clear_pending()
    from app.core.approval import _pending
    with patch("app.core.approval.time.time", return_value=time.time() - 1000):
        req = _make_request()
    assert req.approval_id in _pending
    result = get_pending(req.approval_id)
    assert result is None
    assert req.approval_id not in _pending


# ---------------------------------------------------------------------------
# requires_approval()
# ---------------------------------------------------------------------------


def test_requires_approval_rollback_always():
    """rollback action always requires approval regardless of other factors."""
    needs, reason = requires_approval(
        action_type="rollback",
        service="sample-app",
        confidence="high",
        blast_radius_score="low",
        criticality=None,
    )
    assert needs is True
    assert "rollback" in reason.lower()


def test_requires_approval_critical_criticality():
    """criticality=critical always requires approval."""
    needs, reason = requires_approval(
        action_type="restart_pod",
        service="core-service",
        confidence="high",
        blast_radius_score="low",
        criticality="critical",
    )
    assert needs is True
    assert "critical" in reason


def test_requires_approval_high_blast_radius():
    """blast_radius=high requires approval."""
    needs, reason = requires_approval(
        action_type="restart_pod",
        service="sample-app",
        confidence="high",
        blast_radius_score="high",
        criticality=None,
    )
    assert needs is True
    assert "blast" in reason.lower() or "high" in reason.lower()


def test_requires_approval_critical_blast_radius():
    """blast_radius=critical requires approval."""
    needs, reason = requires_approval(
        action_type="scale_up",
        service="sample-app",
        confidence="high",
        blast_radius_score="critical",
        criticality=None,
    )
    assert needs is True


def test_requires_approval_low_confidence_destructive():
    """confidence!=high with destructive action requires approval."""
    needs, reason = requires_approval(
        action_type="restart_pod",
        service="sample-app",
        confidence="medium",
        blast_radius_score="low",
        criticality=None,
    )
    assert needs is True
    assert "confidence" in reason.lower() or "medium" in reason


def test_no_approval_required_normal_conditions():
    """High confidence, low blast radius, non-critical → no approval needed."""
    needs, reason = requires_approval(
        action_type="restart_pod",
        service="sample-app",
        confidence="high",
        blast_radius_score="low",
        criticality=None,
    )
    assert needs is False
    assert reason == ""


def test_no_approval_for_non_destructive_actions():
    """notify and no_action never require approval."""
    for action in ("notify", "no_action"):
        needs, _ = requires_approval(
            action_type=action,
            service="sample-app",
            confidence="low",
            blast_radius_score="critical",
            criticality="critical",
        )
        assert needs is False, f"Expected no approval for {action}"


def test_requires_approval_medium_blast_no_criticality_high_confidence():
    """medium blast radius with high confidence → no approval needed."""
    needs, _ = requires_approval(
        action_type="scale_up",
        service="sample-app",
        confidence="high",
        blast_radius_score="medium",
        criticality=None,
    )
    # medium blast radius is not in (high, critical) → no blast radius block
    assert needs is False


# ---------------------------------------------------------------------------
# ApprovalRequest.is_expired()
# ---------------------------------------------------------------------------


def test_is_expired_false_for_fresh_request():
    """Freshly created request is not expired."""
    _clear_pending()
    req = _make_request()
    assert req.is_expired() is False


def test_is_expired_true_for_old_request():
    """Request with expires_at in the past is expired."""
    from app.core.approval import _sign
    req = ApprovalRequest(
        approval_id="test-id",
        incident_id="inc-test",
        service="sample-app",
        action_type="restart_pod",
        target="sample-app",
        reason="test",
        expires_at=time.time() - 1,
        signed_token=_sign("test-id", "inc-test", time.time() - 1),
    )
    assert req.is_expired() is True


def test_to_dict_excludes_signed_token():
    """to_dict() does not include signed_token for security."""
    _clear_pending()
    req = _make_request()
    d = req.to_dict()
    assert "signed_token" not in d
    assert "approval_id" in d
    assert "incident_id" in d
    assert "service" in d
    assert "action_type" in d

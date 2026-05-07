"""Tests for Phase 11d — Slack notifications (app/integrations/slack.py)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.approval import ApprovalRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_approval_request(action_type="restart_pod", service="sample-app"):
    """Build a minimal ApprovalRequest for testing."""
    import time
    from app.core.approval import _sign
    approval_id = "test-approval-id-123"
    incident_id = "test-incident-id-456"
    expires_at = time.time() + 300
    token = _sign(approval_id, incident_id, expires_at)
    return ApprovalRequest(
        approval_id=approval_id,
        incident_id=incident_id,
        service=service,
        action_type=action_type,
        target=service,
        reason="test approval reason",
        expires_at=expires_at,
        signed_token=token,
    )


# ---------------------------------------------------------------------------
# notify_approval_required — no-op when URL unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_approval_required_noop_when_no_url():
    """No Slack post when SLACK_WEBHOOK_URL is empty."""
    from app.integrations.slack import notify_approval_required

    req = _make_approval_request()
    with patch("app.integrations.slack._post") as mock_post:
        with patch("app.utils.config.settings") as mock_settings:
            mock_settings.slack_webhook_url = ""
            await notify_approval_required(req, confidence_score=8)

    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_approval_required_posts_when_url_set():
    """Slack post is triggered when SLACK_WEBHOOK_URL is configured."""
    from app.integrations.slack import notify_approval_required

    req = _make_approval_request()
    captured_payloads = []

    def _capture(payload):
        captured_payloads.append(payload)

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
         patch("app.integrations.slack._post", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        mock_settings.approval_expiry_seconds = 300
        await notify_approval_required(
            req,
            confidence_score=8,
            confidence_breakdown=["+2 runtime_crash", "+2 severity_high"],
            blast_radius_score="low",
            base_url="http://localhost:8001",
        )

    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    assert "blocks" in payload


@pytest.mark.asyncio
async def test_notify_approval_required_payload_structure():
    """Slack Block Kit payload has header, sections, and action buttons."""
    from app.integrations.slack import notify_approval_required

    req = _make_approval_request()
    captured_payloads = []

    def _capture(payload):
        captured_payloads.append(payload)

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
         patch("app.integrations.slack._post", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        mock_settings.approval_expiry_seconds = 300
        await notify_approval_required(
            req,
            confidence_score=8,
            blast_radius_score="medium",
            base_url="http://localhost:8001",
        )

    blocks = captured_payloads[0]["blocks"]
    block_types = [b["type"] for b in blocks]

    # Must have header, section, context, and actions
    assert "header" in block_types
    assert "section" in block_types
    assert "actions" in block_types

    # Actions block must have Approve + Reject buttons
    actions_block = next(b for b in blocks if b["type"] == "actions")
    button_texts = [e["text"]["text"] for e in actions_block["elements"]]
    assert any("Approve" in t for t in button_texts)
    assert any("Reject" in t for t in button_texts)


@pytest.mark.asyncio
async def test_notify_approval_includes_token_in_urls():
    """Approve/reject button URLs include the signed token."""
    from app.integrations.slack import notify_approval_required

    req = _make_approval_request()
    captured_payloads = []

    def _capture(payload):
        captured_payloads.append(payload)

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
         patch("app.integrations.slack._post", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        mock_settings.approval_expiry_seconds = 300
        await notify_approval_required(req, confidence_score=7, base_url="http://agent:8001")

    actions_block = next(
        b for b in captured_payloads[0]["blocks"] if b["type"] == "actions"
    )
    # Both button URLs should contain the signed token
    for elem in actions_block["elements"]:
        url = elem.get("url", "")
        assert req.signed_token in url, f"Token not in URL: {url}"


@pytest.mark.asyncio
async def test_notify_approval_includes_breakdown_when_provided():
    """Confidence breakdown section is included when breakdown list is non-empty."""
    from app.integrations.slack import notify_approval_required

    req = _make_approval_request()
    captured_payloads = []

    def _capture(payload):
        captured_payloads.append(payload)

    breakdown = ["+2 error_type=runtime_crash", "+2 severity=high"]
    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
         patch("app.integrations.slack._post", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        mock_settings.approval_expiry_seconds = 300
        await notify_approval_required(
            req,
            confidence_score=6,
            confidence_breakdown=breakdown,
            base_url="http://agent:8001",
        )

    full_text = json.dumps(captured_payloads[0])
    assert "runtime_crash" in full_text
    assert "severity=high" in full_text


# ---------------------------------------------------------------------------
# notify_incident — general incident notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_incident_noop_when_no_url():
    """No Slack post when SLACK_WEBHOOK_URL is empty."""
    from app.integrations.slack import notify_incident

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("app.integrations.slack._post") as mock_post:
        mock_settings.slack_webhook_url = ""
        await notify_incident("sample-app", "OOM kill", "restart_pod", 7)

    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_incident_posts_when_url_set():
    """Incident notification is sent when Slack URL is configured."""
    from app.integrations.slack import notify_incident

    captured_payloads = []

    def _capture(payload):
        captured_payloads.append(payload)

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
         patch("app.integrations.slack._post", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        await notify_incident(
            service="sample-app",
            root_cause="OOM kill",
            action_type="restart_pod",
            confidence_score=8,
            incident_id="inc-123",
        )

    assert len(captured_payloads) == 1
    assert "blocks" in captured_payloads[0]


@pytest.mark.asyncio
async def test_notify_incident_includes_service_and_action():
    """Incident notification text includes service name and action type."""
    from app.integrations.slack import notify_incident

    captured_payloads = []

    def _capture(payload):
        captured_payloads.append(payload)

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
         patch("app.integrations.slack._post", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        await notify_incident(
            service="payment-api",
            root_cause="Connection pool exhausted",
            action_type="scale_up",
            confidence_score=9,
            incident_id="inc-456",
        )

    full_text = json.dumps(captured_payloads[0])
    assert "payment-api" in full_text
    assert "scale_up" in full_text
    assert "Connection pool" in full_text


@pytest.mark.asyncio
async def test_notify_incident_emoji_mapping():
    """Different action types get appropriate emojis."""
    from app.integrations.slack import notify_incident

    action_emoji_map = {
        "restart_pod": "🔄",
        "rollback": "⏪",
        "scale_up": "📈",
        "notify": "🔔",
        "no_action": "✅",
        "unknown_action": "⚠️",
    }

    for action, emoji in action_emoji_map.items():
        captured = []

        def _capture(payload, _e=emoji):
            captured.append(payload)

        with patch("app.integrations.slack.settings") as mock_settings, \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))), \
             patch("app.integrations.slack._post", side_effect=_capture):
            mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
            await notify_incident("svc", "root cause", action, 5)

        full_text = json.dumps(captured[0], ensure_ascii=False)
        assert emoji in full_text, f"Expected emoji {emoji} for action {action}"


# ---------------------------------------------------------------------------
# _post — synchronous HTTP helper
# ---------------------------------------------------------------------------


def test_post_noop_when_no_url():
    """_post does nothing when slack_webhook_url is empty."""
    from app.integrations.slack import _post
    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("urllib.request.urlopen") as mock_urlopen:
        mock_settings.slack_webhook_url = ""
        _post({"blocks": []})
    mock_urlopen.assert_not_called()


def test_post_sends_correct_payload():
    """_post sends JSON-encoded payload with correct content-type."""
    from app.integrations.slack import _post

    captured_requests = []
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    def _capture(req, timeout=None):
        captured_requests.append(req)
        return mock_resp

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("urllib.request.urlopen", side_effect=_capture):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        _post({"text": "hello"})

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.get_header("Content-type") == "application/json"
    sent_body = json.loads(req.data)
    assert sent_body == {"text": "hello"}


def test_post_logs_non_200_without_raising():
    """_post logs warning on non-200 response but does not raise."""
    from app.integrations.slack import _post

    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("urllib.request.urlopen", return_value=mock_resp):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        # Should not raise
        _post({"text": "test"})


def test_post_logs_network_error_without_raising():
    """_post logs warning on network error but does not raise."""
    from app.integrations.slack import _post

    with patch("app.integrations.slack.settings") as mock_settings, \
         patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
        # Should not raise
        _post({"text": "test"})

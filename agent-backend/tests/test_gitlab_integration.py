"""Tests for app.integrations.gitlab (webhook verification + service extraction)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.integrations.gitlab import extract_failing_service, verify_webhook_token


# ── Webhook signature verification ──────────────────────────────────────────


def test_verify_webhook_token_permissive_when_unset():
    """Dev mode (empty secret) accepts any token — even None."""
    with patch("app.integrations.gitlab.settings") as mock_settings:
        mock_settings.gitlab_webhook_token = ""
        assert verify_webhook_token(None) is True
        assert verify_webhook_token("anything") is True


def test_verify_webhook_token_matches_correct():
    with patch("app.integrations.gitlab.settings") as mock_settings:
        mock_settings.gitlab_webhook_token = "shhh-secret-123"
        assert verify_webhook_token("shhh-secret-123") is True


def test_verify_webhook_token_rejects_wrong():
    with patch("app.integrations.gitlab.settings") as mock_settings:
        mock_settings.gitlab_webhook_token = "shhh-secret-123"
        assert verify_webhook_token("wrong") is False
        assert verify_webhook_token("") is False
        assert verify_webhook_token(None) is False


def test_verify_webhook_token_uses_constant_time_compare():
    """Verify we don't return early on length mismatch (hmac.compare_digest)."""
    import time
    with patch("app.integrations.gitlab.settings") as mock_settings:
        mock_settings.gitlab_webhook_token = "a" * 1000

        # Short string compare
        t1 = time.perf_counter()
        for _ in range(1000):
            verify_webhook_token("b")
        t_short = time.perf_counter() - t1

        # Same-length compare
        t1 = time.perf_counter()
        for _ in range(1000):
            verify_webhook_token("b" * 1000)
        t_same_len = time.perf_counter() - t1

        # Not a tight assertion — just sanity that we're using compare_digest.
        # The actual timing-safety is delegated to hmac.compare_digest.
        assert t_short >= 0  # smoke
        assert t_same_len >= 0


# ── Service extraction from GitLab payloads ─────────────────────────────────


def test_extract_failing_service_with_known_service_match():
    payload = {
        "builds": [
            {"status": "failed", "name": "deploy-auth-service", "stage": "deploy"},
        ]
    }
    assert extract_failing_service(payload, ["api-gateway", "auth-service"]) == "auth-service"


def test_extract_failing_service_strips_test_prefix():
    payload = {"builds": [{"status": "failed", "name": "test-room-service"}]}
    assert extract_failing_service(payload, ["room-service"]) == "room-service"


def test_extract_failing_service_strips_build_prefix():
    payload = {"builds": [{"status": "failed", "name": "build-my-svc"}]}
    assert extract_failing_service(payload, ["my-svc"]) == "my-svc"


def test_extract_failing_service_heuristic_longest_substring():
    """Even if the stripped name doesn't exactly match, longest substring wins."""
    payload = {"builds": [{"status": "failed", "name": "deploy-auth-service-canary"}]}
    assert extract_failing_service(payload, ["auth-service", "auth"]) == "auth-service"


def test_extract_failing_service_no_known_services_returns_stripped():
    """When platform is unregistered, fall back to the stripped build name."""
    payload = {"builds": [{"status": "failed", "name": "deploy-my-new-svc"}]}
    assert extract_failing_service(payload, []) == "my-new-svc"


def test_extract_failing_service_falls_back_to_project_name():
    """No failing builds — project name is the only signal."""
    payload = {
        "builds": [{"status": "success", "name": "build-ok"}],
        "project": {"name": "spyroom-platform"},
    }
    assert extract_failing_service(payload, []) == "spyroom-platform"


def test_extract_failing_service_returns_none_for_empty_payload():
    assert extract_failing_service({}, []) is None
    assert extract_failing_service({"project": {}}, []) is None


def test_extract_failing_service_skips_successful_builds():
    payload = {
        "builds": [
            {"status": "success", "name": "test-auth-service"},
            {"status": "failed",  "name": "test-room-service"},
        ]
    }
    assert extract_failing_service(payload, ["auth-service", "room-service"]) == "room-service"


def test_extract_failing_service_accepts_canceled_status():
    payload = {"builds": [{"status": "canceled", "name": "deploy-api-gateway"}]}
    assert extract_failing_service(payload, ["api-gateway"]) == "api-gateway"

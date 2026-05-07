"""Tests for Phase 11c — pipeline failure webhook (app/api/v1/webhooks.py)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from app.api.v1.webhooks import (
    _extract_service_github,
    _extract_service_gitlab,
    handle_pipeline_failure,
)


# ---------------------------------------------------------------------------
# Service extraction helpers
# ---------------------------------------------------------------------------


class TestExtractServiceGitlab:
    def test_extracts_from_project_name(self):
        payload = {"project": {"name": "sample-app"}, "builds": []}
        assert _extract_service_gitlab(payload) == "sample-app"

    def test_normalises_underscores_to_hyphens(self):
        payload = {"project": {"name": "my_service"}}
        assert _extract_service_gitlab(payload) == "my-service"

    def test_lowercases_name(self):
        payload = {"project": {"name": "SAMPLE-APP"}}
        assert _extract_service_gitlab(payload) == "sample-app"

    def test_falls_back_to_failing_build_name(self):
        payload = {
            "project": {},
            "builds": [
                {"status": "success", "name": "build-other"},
                {"status": "failed",  "name": "test-sample-app"},
            ],
        }
        assert _extract_service_gitlab(payload) == "sample-app"

    def test_strips_build_prefix(self):
        payload = {
            "project": {},
            "builds": [{"status": "failed", "name": "build-my-svc"}],
        }
        assert _extract_service_gitlab(payload) == "my-svc"

    def test_strips_deploy_prefix(self):
        payload = {
            "project": {},
            "builds": [{"status": "failed", "name": "deploy-payment-api"}],
        }
        assert _extract_service_gitlab(payload) == "payment-api"

    def test_strips_push_prefix(self):
        payload = {
            "project": {},
            "builds": [{"status": "failed", "name": "push-auth-service"}],
        }
        assert _extract_service_gitlab(payload) == "auth-service"

    def test_returns_raw_build_name_without_known_prefix(self):
        payload = {
            "project": {},
            "builds": [{"status": "failed", "name": "my-job"}],
        }
        assert _extract_service_gitlab(payload) == "my-job"

    def test_returns_none_when_no_project_or_builds(self):
        assert _extract_service_gitlab({}) is None

    def test_returns_none_when_no_failed_builds(self):
        payload = {
            "project": {},
            "builds": [{"status": "success", "name": "build-ok"}],
        }
        assert _extract_service_gitlab(payload) is None


class TestExtractServiceGithub:
    def test_extracts_from_repository_name(self):
        payload = {
            "action": "completed",
            "workflow_run": {
                "conclusion": "failure",
                "head_sha": "abc1234",
                "repository": {"name": "sample-app"},
            },
        }
        assert _extract_service_github(payload) == "sample-app"

    def test_normalises_underscores(self):
        payload = {"workflow_run": {"repository": {"name": "my_service"}}}
        assert _extract_service_github(payload) == "my-service"

    def test_returns_none_when_no_repo_name(self):
        payload = {"workflow_run": {"repository": {}}}
        assert _extract_service_github(payload) is None

    def test_returns_none_when_no_workflow_run(self):
        assert _extract_service_github({}) is None


# ---------------------------------------------------------------------------
# handle_pipeline_failure — request routing
# ---------------------------------------------------------------------------


def _make_request(body: dict) -> Request:
    """Build a minimal FastAPI Request from a JSON body dict."""
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/webhook/pipeline-failure",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    return Request(scope, receive=receive)


@pytest.mark.asyncio
async def test_gitlab_pipeline_failure_accepted():
    """GitLab pipeline failure payload is accepted and triggers analysis."""
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {"status": "failed"},
        "project": {"name": "sample-app"},
        "commit": {"id": "abc12345678"},
        "builds": [],
    }
    request = _make_request(payload)

    with patch("app.core.agent.run_analysis", new_callable=AsyncMock) as mock_analyze, \
         patch("asyncio.create_task") as mock_task:
        mock_analyze.return_value = MagicMock(root_cause="test", incident_id="inc-1",
                                               proposed_action={"type": "notify"})
        response = await handle_pipeline_failure(request)

    assert isinstance(response, JSONResponse)
    data = json.loads(response.body)
    assert data["accepted"] is True
    assert data["service"] == "sample-app"
    assert data["commit_sha"] == "abc12345"  # first 8 chars


@pytest.mark.asyncio
async def test_github_workflow_failure_accepted():
    """GitHub Actions workflow failure payload is accepted."""
    payload = {
        "action": "completed",
        "workflow_run": {
            "conclusion": "failure",
            "head_sha": "deadbeef12345678",
            "name": "CI",
            "repository": {"name": "payment-api"},
        },
    }
    request = _make_request(payload)

    with patch("asyncio.create_task"):
        response = await handle_pipeline_failure(request)

    data = json.loads(response.body)
    assert data["accepted"] is True
    assert data["service"] == "payment-api"
    assert data["commit_sha"] == "deadbeef"


@pytest.mark.asyncio
async def test_non_pipeline_gitlab_event_skipped():
    """Non-pipeline GitLab events (e.g. push) are skipped."""
    payload = {"object_kind": "push", "project": {"name": "sample-app"}}
    request = _make_request(payload)
    response = await handle_pipeline_failure(request)
    data = json.loads(response.body)
    assert data["skipped"] is True
    assert "not a pipeline event" in data["reason"]


@pytest.mark.asyncio
async def test_gitlab_non_failed_status_skipped():
    """GitLab pipelines with status!=failed/canceled are skipped."""
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {"status": "success"},
        "project": {"name": "sample-app"},
    }
    request = _make_request(payload)
    response = await handle_pipeline_failure(request)
    data = json.loads(response.body)
    assert data["skipped"] is True
    assert "success" in data["reason"]


@pytest.mark.asyncio
async def test_github_non_completed_action_skipped():
    """GitHub workflow that is not completed is skipped."""
    payload = {
        "action": "requested",
        "workflow_run": {"conclusion": None, "repository": {"name": "sample-app"}},
    }
    request = _make_request(payload)
    response = await handle_pipeline_failure(request)
    data = json.loads(response.body)
    assert data["skipped"] is True
    assert "not completed" in data["reason"]


@pytest.mark.asyncio
async def test_github_non_failure_conclusion_skipped():
    """GitHub workflow with conclusion=success is skipped."""
    payload = {
        "action": "completed",
        "workflow_run": {
            "conclusion": "success",
            "repository": {"name": "sample-app"},
        },
    }
    request = _make_request(payload)
    response = await handle_pipeline_failure(request)
    data = json.loads(response.body)
    assert data["skipped"] is True


@pytest.mark.asyncio
async def test_unrecognised_webhook_format_skipped():
    """Unknown webhook formats are skipped gracefully."""
    payload = {"some_random_key": "value"}
    request = _make_request(payload)
    response = await handle_pipeline_failure(request)
    data = json.loads(response.body)
    assert data["skipped"] is True
    assert "unrecognised" in data["reason"]


@pytest.mark.asyncio
async def test_invalid_json_returns_400():
    """Invalid JSON in request body returns 400."""
    from fastapi import HTTPException

    async def receive():
        return {"type": "http.request", "body": b"not valid json", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/webhook/pipeline-failure",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    request = Request(scope, receive=receive)

    with pytest.raises(HTTPException) as exc_info:
        await handle_pipeline_failure(request)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_gitlab_canceled_pipeline_accepted():
    """GitLab canceled pipelines (as well as failed) trigger analysis."""
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {"status": "canceled"},
        "project": {"name": "auth-service"},
        "commit": {"id": "cafe1234"},
        "builds": [],
    }
    request = _make_request(payload)

    with patch("asyncio.create_task"):
        response = await handle_pipeline_failure(request)

    data = json.loads(response.body)
    assert data["accepted"] is True
    assert data["service"] == "auth-service"


@pytest.mark.asyncio
async def test_response_includes_message():
    """Response body includes a human-readable message field."""
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {"status": "failed"},
        "project": {"name": "sample-app"},
        "commit": {"id": "abc1234"},
    }
    request = _make_request(payload)

    with patch("asyncio.create_task"):
        response = await handle_pipeline_failure(request)

    data = json.loads(response.body)
    assert "message" in data
    assert "sample-app" in data["message"]


@pytest.mark.asyncio
async def test_missing_service_name_skipped():
    """When service name cannot be extracted, webhook is skipped."""
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {"status": "failed"},
        "project": {},   # No name
        "builds": [],    # No failing builds
    }
    request = _make_request(payload)
    response = await handle_pipeline_failure(request)
    data = json.loads(response.body)
    assert data["skipped"] is True
    assert "service" in data["reason"].lower()

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import AnalysisResult, ParsedLog

client = TestClient(app)

_MOCK_RESULT = AnalysisResult(
    service="sample-app",
    environment="dev",
    root_cause="connection refused to elasticsearch",
    root_causes=[{"cause": "connection refused to elasticsearch", "confidence": 0.9}],
    suggestion="restart elasticsearch",
    confidence_hint="high",
    confidence_score=8,
    confidence_source="signal",
    parsed_log=ParsedLog(
        error_type="dependency_error",
        severity="high",
        key_events=["GET /api → 500"],
        summary="connection refused to elasticsearch",
    ),
    raw_evidence=["GET /api → 500"],
    log_summary={},
    proposed_action={"type": "notify", "target": "elasticsearch", "reason": "dep error"},
    causality_verified=True,
    causality_target="elasticsearch",
)


# ── /health ──────────────────────────────────────────────────────────────────

def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── /api/v1/analyze ───────────────────────────────────────────────────────────

def test_analyze_returns_200_with_valid_request():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _MOCK_RESULT
        response = client.post(
            "/api/v1/analyze",
            json={"service": "sample-app", "environment": "dev", "lookback_minutes": 5},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "sample-app"
    assert body["root_cause"] == "connection refused to elasticsearch"
    assert body["confidence_hint"] == "high"


def test_analyze_returns_root_causes_list():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _MOCK_RESULT
        response = client.post(
            "/api/v1/analyze",
            json={"service": "sample-app", "environment": "dev"},
        )
    body = response.json()
    assert isinstance(body["root_causes"], list)
    assert body["root_causes"][0]["confidence"] == 0.9


def test_analyze_returns_proposed_action():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _MOCK_RESULT
        response = client.post(
            "/api/v1/analyze",
            json={"service": "sample-app", "environment": "dev"},
        )
    body = response.json()
    assert body["proposed_action"]["type"] == "notify"
    assert body["causality_verified"] is True
    assert body["causality_target"] == "elasticsearch"


def test_analyze_returns_500_on_value_error():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = ValueError("No logs found for service='ghost-svc'")
        response = client.post(
            "/api/v1/analyze",
            json={"service": "ghost-svc", "environment": "dev", "lookback_minutes": 5},
        )
    assert response.status_code == 500
    assert "ghost-svc" in response.json()["detail"]


def test_analyze_returns_500_on_unexpected_error():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError("elasticsearch unreachable")
        response = client.post(
            "/api/v1/analyze",
            json={"service": "sample-app", "environment": "dev"},
        )
    assert response.status_code == 500


def test_analyze_invalid_environment_returns_422():
    response = client.post(
        "/api/v1/analyze",
        json={"service": "sample-app", "environment": "production123"},
    )
    assert response.status_code == 422


def test_analyze_missing_service_returns_422():
    response = client.post("/api/v1/analyze", json={"environment": "dev"})
    assert response.status_code == 422


def test_analyze_default_environment_is_dev():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _MOCK_RESULT
        client.post("/api/v1/analyze", json={"service": "sample-app"})
    call_args = mock_run.call_args[0][0]
    assert call_args.environment.value == "dev"


def test_analyze_default_lookback_is_30():
    with patch("app.api.routes.run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _MOCK_RESULT
        client.post("/api/v1/analyze", json={"service": "sample-app"})
    call_args = mock_run.call_args[0][0]
    assert call_args.lookback_minutes == 30

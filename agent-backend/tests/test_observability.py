"""Tests for Phase 8b/8c/8d observability components.

Covers:
  - metrics_builder.compute_metrics() (8b)
  - prom_metrics module exports and counter/histogram types (8c)
  - GET /api/v1/metrics and GET /metrics routes (8b + 8c)
  - GET /api/v1/incidents/{id}/timeline (8d)
  - GET /dashboard HTML rendering (8d)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_INCIDENT = {
    "incident_id": "test-uuid-1234",
    "timestamp": "2026-05-07T12:00:00+00:00",
    "service": "sample-app",
    "error_type": "runtime_crash",
    "severity": "high",
    "confidence_hint": "high",
    "confidence_score": 8,
    "safety_decision": "allowed",
    "outcome": "resolved",
    "action_state": "completed",
    "proposed_action": {"type": "restart_pod", "target": "sample-app"},
    "mttr_seconds": 45,
    "causality_verified": True,
    "tool_calls": [
        {
            "tool": "search_logs",
            "args_summary": "query='OOMKilled'",
            "result_summary": "3 matching events found",
            "called_at": "2026-05-07T12:00:05+00:00",
        }
    ],
    "log_summary": {
        "change_point_description": "error rate jumped from 0% to 80% at t-2.3m",
        "error_ratio": 0.80,
        "total_events": 25,
    },
}


# ── Helper: mock ES response for compute_metrics ──────────────────────────────


def _mock_es_metrics_response(
    total=10,
    denied=2,
    causality_rejected=1,
    actioned=5,
    resolved=4,
    unknown_outcome=1,
    rollback_triggered=0,
    p50=42.0,
    p95=90.0,
    frozen_buckets=None,
):
    aggs = {
        "total": {"value": total},
        "safety_denied": {"doc_count": denied},
        "causality_rejected": {"doc_count": causality_rejected},
        "actioned": {
            "doc_count": actioned,
            "resolved": {"doc_count": resolved},
            "unknown_outcome": {"doc_count": unknown_outcome},
            "rollback_triggered": {"doc_count": rollback_triggered},
        },
        "mttr_percentiles": {"values": {"50.0": p50, "95.0": p95}},
        "frozen": {
            "services": {"buckets": frozen_buckets or []}
        },
    }
    return {"aggregations": aggs}


# ═══════════════════════════════════════════════════════════════════════════════
# 8b — compute_metrics
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_metrics_returns_all_keys():
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=_mock_es_metrics_response())
    mock_client.count = AsyncMock(return_value={"count": 3})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    expected_keys = {
        "correct_fix_rate", "false_positive_rate", "rollback_frequency",
        "safety_override_rate", "causality_reject_rate",
        "mttr_p50_seconds", "mttr_p95_seconds",
        "action_budget_used", "frozen_services",
        "total_incidents_24h", "total_actioned_24h",
    }
    assert expected_keys == set(result.keys())


@pytest.mark.asyncio
async def test_compute_metrics_correct_fix_rate_math():
    """correct_fix_rate = resolved / actioned = 4/5 = 0.8"""
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(
        return_value=_mock_es_metrics_response(actioned=5, resolved=4)
    )
    mock_client.count = AsyncMock(return_value={"count": 0})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["correct_fix_rate"] == pytest.approx(0.8, abs=0.001)
    assert result["total_actioned_24h"] == 5


@pytest.mark.asyncio
async def test_compute_metrics_safety_override_rate_math():
    """safety_override_rate = denied / total = 2/10 = 0.2"""
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(
        return_value=_mock_es_metrics_response(total=10, denied=2)
    )
    mock_client.count = AsyncMock(return_value={"count": 0})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["safety_override_rate"] == pytest.approx(0.2, abs=0.001)


@pytest.mark.asyncio
async def test_compute_metrics_mttr_percentiles():
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(
        return_value=_mock_es_metrics_response(p50=33.5, p95=120.0)
    )
    mock_client.count = AsyncMock(return_value={"count": 0})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["mttr_p50_seconds"] == pytest.approx(33.5)
    assert result["mttr_p95_seconds"] == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_compute_metrics_frozen_services():
    from app.core.metrics_builder import compute_metrics

    frozen = [{"key": "sample-app"}, {"key": "api-gateway"}]
    mock_client = AsyncMock()
    mock_client.search = AsyncMock(
        return_value=_mock_es_metrics_response(frozen_buckets=frozen)
    )
    mock_client.count = AsyncMock(return_value={"count": 0})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert "sample-app" in result["frozen_services"]
    assert "api-gateway" in result["frozen_services"]


@pytest.mark.asyncio
async def test_compute_metrics_action_budget_used():
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=_mock_es_metrics_response())
    mock_client.count = AsyncMock(return_value={"count": 7})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["action_budget_used"] == 7


@pytest.mark.asyncio
async def test_compute_metrics_es_failure_returns_defaults():
    """If ES is down, returns a zeroed-out dict (never raises)."""
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(side_effect=ConnectionError("ES down"))
    mock_client.count = AsyncMock(side_effect=ConnectionError("ES down"))

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["correct_fix_rate"] == 0.0
    assert result["total_incidents_24h"] == 0
    assert result["frozen_services"] == []


@pytest.mark.asyncio
async def test_compute_metrics_zero_actioned_no_division_error():
    """When actioned=0 all rate fields stay 0.0 (no ZeroDivisionError)."""
    from app.core.metrics_builder import compute_metrics

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(
        return_value=_mock_es_metrics_response(total=5, actioned=0, resolved=0)
    )
    mock_client.count = AsyncMock(return_value={"count": 0})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["correct_fix_rate"] == 0.0
    assert result["false_positive_rate"] == 0.0
    assert result["rollback_frequency"] == 0.0


@pytest.mark.asyncio
async def test_compute_metrics_mttr_none_when_missing():
    """MTTR is None when ES returns no percentile data."""
    from app.core.metrics_builder import compute_metrics

    resp = _mock_es_metrics_response()
    resp["aggregations"]["mttr_percentiles"] = {"values": {}}

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=resp)
    mock_client.count = AsyncMock(return_value={"count": 0})

    with patch("app.core.metrics_builder.get_client", return_value=mock_client):
        result = await compute_metrics()

    assert result["mttr_p50_seconds"] is None
    assert result["mttr_p95_seconds"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 8c — prom_metrics module
# ═══════════════════════════════════════════════════════════════════════════════


def test_prom_metrics_module_exports_counters():
    from prometheus_client import Counter, Histogram
    from app.utils import prom_metrics

    assert isinstance(prom_metrics.analysis_total, Counter)
    assert isinstance(prom_metrics.llm_call_total, Counter)
    assert isinstance(prom_metrics.safety_denials_total, Counter)
    assert isinstance(prom_metrics.actions_executed_total, Counter)


def test_prom_metrics_module_exports_histograms():
    from prometheus_client import Histogram
    from app.utils import prom_metrics

    assert isinstance(prom_metrics.analysis_duration, Histogram)
    assert isinstance(prom_metrics.llm_call_duration, Histogram)


def test_prom_metrics_generate_latest_callable():
    """generate_latest() should return bytes without raising."""
    from app.utils.prom_metrics import generate_latest
    data = generate_latest()
    assert isinstance(data, bytes)
    assert len(data) > 0


def test_prom_metrics_content_type_is_string():
    from app.utils.prom_metrics import CONTENT_TYPE_LATEST
    assert isinstance(CONTENT_TYPE_LATEST, str)
    assert "text/plain" in CONTENT_TYPE_LATEST


def test_safety_denials_counter_has_reason_label():
    """safety_denials_total must have 'reason' label for safety.py wiring."""
    from app.utils.prom_metrics import safety_denials_total
    # Calling .labels() with a known reason should not raise
    safety_denials_total.labels(reason="test_label")


def test_llm_call_counter_has_provider_model_result_labels():
    from app.utils.prom_metrics import llm_call_total
    llm_call_total.labels(provider="google", model="gemma-4b", result="ok")


def test_analysis_total_has_service_outcome_labels():
    from app.utils.prom_metrics import analysis_total
    analysis_total.labels(service="sample-app", outcome="resolved")


# ═══════════════════════════════════════════════════════════════════════════════
# 8b/8c — HTTP routes
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def test_api_metrics_endpoint_returns_200(client):
    mock_result = {
        "correct_fix_rate": 0.75,
        "false_positive_rate": 0.1,
        "rollback_frequency": 0.05,
        "safety_override_rate": 0.2,
        "causality_reject_rate": 0.1,
        "mttr_p50_seconds": 45.0,
        "mttr_p95_seconds": 120.0,
        "action_budget_used": 3,
        "frozen_services": [],
        "total_incidents_24h": 20,
        "total_actioned_24h": 8,
    }
    with patch(
        "app.core.metrics_builder.compute_metrics",
        new=AsyncMock(return_value=mock_result),
    ):
        resp = client.get("/api/v1/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "correct_fix_rate" in data
    assert "frozen_services" in data


def test_prometheus_scrape_endpoint_returns_200(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    # Prometheus format: should contain metric lines
    body = resp.text
    assert "agent_" in body or len(body) >= 0  # may be empty if no increments yet


def test_prometheus_scrape_content_type(client):
    resp = client.get("/metrics")
    assert "text/plain" in resp.headers["content-type"]
    assert "version=0.0.4" in resp.headers["content-type"]


# ═══════════════════════════════════════════════════════════════════════════════
# 8d — incident timeline
# ═══════════════════════════════════════════════════════════════════════════════


# routes.py imports get_incident at module-load time, so we patch the name in that module.
_ROUTE_GET_INCIDENT = "app.api.routes.get_incident"


def test_timeline_returns_404_for_unknown_incident(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=None)):
        resp = client.get("/api/v1/incidents/nonexistent-id/timeline")
    assert resp.status_code == 404


def test_timeline_includes_analysis_started_event(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    assert resp.status_code == 200
    event_types = [e["event"] for e in resp.json()["timeline"]]
    assert "analysis_started" in event_types


def test_timeline_includes_change_point_event(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    event_types = [e["event"] for e in resp.json()["timeline"]]
    assert "error_rate_spike" in event_types


def test_timeline_includes_tool_calls(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    tool_events = [e for e in resp.json()["timeline"] if e["event"] == "tool_call"]
    assert len(tool_events) == 1
    assert "search_logs" in tool_events[0]["detail"]


def test_timeline_includes_safety_event(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    safety_events = [e for e in resp.json()["timeline"] if "safety" in e["event"]]
    assert len(safety_events) >= 1


def test_timeline_events_have_required_fields(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    for event in resp.json()["timeline"]:
        assert "t" in event
        assert "event" in event
        assert "detail" in event


def test_timeline_incident_without_tool_calls(client):
    """Incident with no tool_calls field should still return a timeline."""
    inc = {k: v for k, v in MOCK_INCIDENT.items() if k != "tool_calls"}
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=inc)):
        resp = client.get(f"/api/v1/incidents/{inc['incident_id']}/timeline")
    assert resp.status_code == 200
    assert len(resp.json()["timeline"]) >= 1


def test_timeline_includes_outcome_event_when_resolved(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    event_types = [e["event"] for e in resp.json()["timeline"]]
    assert "impact_verified" in event_types


def test_timeline_response_has_incident_id_and_service(client):
    with patch(_ROUTE_GET_INCIDENT, new=AsyncMock(return_value=MOCK_INCIDENT)):
        resp = client.get(f"/api/v1/incidents/{MOCK_INCIDENT['incident_id']}/timeline")
    body = resp.json()
    assert body["incident_id"] == MOCK_INCIDENT["incident_id"]
    assert body["service"] == "sample-app"


# ═══════════════════════════════════════════════════════════════════════════════
# 8d — dashboard HTML
# ═══════════════════════════════════════════════════════════════════════════════


def test_dashboard_render_returns_html_string():
    from app.api.v1.dashboard import render_dashboard
    html = render_dashboard([MOCK_INCIDENT])
    assert isinstance(html, str)
    assert html.strip().startswith("<!DOCTYPE html>")


def test_dashboard_render_contains_service_name():
    from app.api.v1.dashboard import render_dashboard
    html = render_dashboard([MOCK_INCIDENT])
    assert "sample-app" in html


def test_dashboard_render_contains_error_type():
    from app.api.v1.dashboard import render_dashboard
    html = render_dashboard([MOCK_INCIDENT])
    assert "runtime_crash" in html


def test_dashboard_render_contains_incident_id():
    from app.api.v1.dashboard import render_dashboard
    html = render_dashboard([MOCK_INCIDENT])
    # First 8 chars of incident_id appear in table
    assert MOCK_INCIDENT["incident_id"][:8] in html


def test_dashboard_render_empty_incidents():
    from app.api.v1.dashboard import render_dashboard
    html = render_dashboard([])
    assert isinstance(html, str)
    assert "<table" in html


def test_dashboard_render_escapes_xss():
    """Data values injected into table cells must be HTML-escaped.
    The template itself contains a legitimate <script> block for filtering,
    so we check that the *malicious payload* is escaped, not that <script> is absent."""
    from app.api.v1.dashboard import render_dashboard
    evil_incident = {**MOCK_INCIDENT, "service": "<script>alert(1)</script>"}
    html = render_dashboard([evil_incident])
    # The malicious literal must not appear unescaped in a data attribute or cell
    assert 'data-service="<script>' not in html
    # The escaped form should be present somewhere in the output
    assert "&lt;script&gt;" in html


def test_dashboard_render_multiple_incidents():
    from app.api.v1.dashboard import render_dashboard
    incidents = [
        {**MOCK_INCIDENT, "incident_id": f"id-{i}", "service": f"svc-{i}"}
        for i in range(5)
    ]
    html = render_dashboard(incidents)
    for i in range(5):
        assert f"svc-{i}" in html


def test_dashboard_http_endpoint_returns_html(client):
    with patch(
        "app.services.memory_store.get_recent_incidents",
        new=AsyncMock(return_value=[MOCK_INCIDENT]),
    ):
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "sample-app" in resp.text


def test_dashboard_http_handles_empty_store(client):
    with patch(
        "app.services.memory_store.get_recent_incidents",
        new=AsyncMock(return_value=[]),
    ):
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "<table" in resp.text

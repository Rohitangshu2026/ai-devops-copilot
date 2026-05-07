"""Intensive tests for Phase 9d cross-model voting.

Covers:
  - _cross_validate: agreement, disagreement, downgrade to notify
  - Single-model chain: voting skipped gracefully
  - Secondary model failure: primary result returned unchanged
  - _cross_validation key in LLM result propagates to AnalysisResult
  - Only triggered for destructive actions + high/critical severity
  - Low-severity destructive actions bypass cross-validation
  - Agent.run_analysis: cross_validation present in AnalysisResult
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.log_processor.summarizer import LogSummary


# ── Helpers ────────────────────────────────────────────────────────────────────

def _summary() -> LogSummary:
    return LogSummary(
        total_events=20,
        error_count=15,
        warning_count=0,
        unique_endpoints=["/api"],
        error_ratio=0.75,
        deduplicated_events=["GET /api 500"],
        time_span_minutes=5.0,
    )


def _make_llm_result(action: str = "restart_pod", error_type: str = "runtime_crash") -> dict:
    return {
        "root_causes": [{"cause": f"pod crashing due to {error_type}", "confidence": 0.8}],
        "root_cause": "pod crashing",
        "suggestion": "restart the pod",
        "proposed_action": {"type": action, "target": "sample-app", "reason": "test"},
    }


# ── _cross_validate unit tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_validate_agreement_no_downgrade():
    """Both models propose restart_pod → action preserved, agreed=True."""
    from app.llm.client import _cross_validate

    primary = _make_llm_result("restart_pod")
    secondary = _make_llm_result("restart_pod")

    with patch("app.llm.client._analyze_with_model", AsyncMock(return_value=secondary)):
        result = await _cross_validate(
            primary_result=primary,
            primary_model="gemma-4-31b-it",
            chain=["gemma-4-31b-it", "claude-haiku-3-5"],
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            key_events=["crash"],
            summary=_summary(),
            lookback_minutes=10,
        )

    cv = result["_cross_validation"]
    assert cv["agreed"] is True
    assert cv["secondary_action"] == "restart_pod"
    assert result["proposed_action"]["type"] == "restart_pod"
    assert "downgraded_to" not in cv


@pytest.mark.asyncio
async def test_cross_validate_disagreement_downgrades_to_notify():
    """Primary proposes restart_pod, secondary proposes rollback → downgrade to notify."""
    from app.llm.client import _cross_validate

    primary = _make_llm_result("restart_pod")
    secondary = _make_llm_result("rollback")

    with patch("app.llm.client._analyze_with_model", AsyncMock(return_value=secondary)):
        result = await _cross_validate(
            primary_result=primary,
            primary_model="gemma-4-31b-it",
            chain=["gemma-4-31b-it", "claude-haiku-3-5"],
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            key_events=["crash"],
            summary=_summary(),
            lookback_minutes=10,
        )

    cv = result["_cross_validation"]
    assert cv["agreed"] is False
    assert cv["downgraded_to"] == "notify"
    assert result["proposed_action"]["type"] == "notify"
    assert "disagreement" in result["proposed_action"]["reason"]


@pytest.mark.asyncio
async def test_cross_validate_single_model_chain_skipped():
    """Only one model in chain → cross-validation skipped, result unchanged."""
    from app.llm.client import _cross_validate

    primary = _make_llm_result("restart_pod")

    with patch("app.llm.client._analyze_with_model", AsyncMock()) as mock:
        result = await _cross_validate(
            primary_result=primary,
            primary_model="gemma-4-31b-it",
            chain=["gemma-4-31b-it"],  # single model
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            key_events=["crash"],
            summary=_summary(),
            lookback_minutes=10,
        )

    mock.assert_not_called()
    cv = result["_cross_validation"]
    assert cv["skipped"] is True
    assert cv["agreed"] is True
    assert result["proposed_action"]["type"] == "restart_pod"


@pytest.mark.asyncio
async def test_cross_validate_secondary_failure_returns_primary():
    """Secondary model raises → primary result returned, skipped=True."""
    from app.llm.client import _cross_validate

    primary = _make_llm_result("restart_pod")

    with patch("app.llm.client._analyze_with_model",
               AsyncMock(side_effect=Exception("rate limit"))):
        result = await _cross_validate(
            primary_result=primary,
            primary_model="gemma-4-31b-it",
            chain=["gemma-4-31b-it", "claude-haiku-3-5"],
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            key_events=["crash"],
            summary=_summary(),
            lookback_minutes=10,
        )

    cv = result["_cross_validation"]
    assert cv["skipped"] is True
    assert cv["agreed"] is True
    assert result["proposed_action"]["type"] == "restart_pod"


@pytest.mark.asyncio
async def test_cross_validate_records_model_names():
    """Cross-validation dict must record both model names."""
    from app.llm.client import _cross_validate

    primary = _make_llm_result("restart_pod")
    secondary = _make_llm_result("restart_pod")

    with patch("app.llm.client._analyze_with_model", AsyncMock(return_value=secondary)):
        result = await _cross_validate(
            primary_result=primary,
            primary_model="gemma-4-31b-it",
            chain=["gemma-4-31b-it", "claude-haiku-3-5"],
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            key_events=["crash"],
            summary=_summary(),
            lookback_minutes=10,
        )

    cv = result["_cross_validation"]
    assert cv["primary_model"] == "gemma-4-31b-it"
    assert cv["secondary_model"] == "claude-haiku-3-5"


@pytest.mark.asyncio
async def test_cross_validate_records_both_actions():
    """Both primary and secondary action types are recorded for audit."""
    from app.llm.client import _cross_validate

    primary = _make_llm_result("scale_up")
    secondary = _make_llm_result("restart_pod")  # different

    with patch("app.llm.client._analyze_with_model", AsyncMock(return_value=secondary)):
        result = await _cross_validate(
            primary_result=primary,
            primary_model="gpt-4o-mini",
            chain=["gpt-4o-mini", "gemma-4-31b-it"],
            service="api-gateway",
            environment="prod",
            error_type="runtime_crash",
            severity="critical",
            key_events=["oom"],
            summary=_summary(),
            lookback_minutes=10,
        )

    cv = result["_cross_validation"]
    assert cv["primary_action"] == "scale_up"
    assert cv["secondary_action"] == "restart_pod"
    assert cv["agreed"] is False


# ── analyze() integration: when cross-validation is triggered ─────────────────

@pytest.mark.asyncio
async def test_analyze_cross_validation_not_triggered_for_notify():
    """notify action → cross-validation NOT run even with high severity."""
    from app.llm.client import analyze

    llm_result = _make_llm_result("notify")  # not destructive
    with (
        patch("app.llm.client._analyze_with_model", AsyncMock(return_value=llm_result)),
        patch("app.llm.client._cross_validate", AsyncMock()) as mock_cv,
    ):
        result = await analyze(
            service="svc",
            environment="dev",
            error_type="dependency_error",
            severity="critical",
            key_events=["connection refused"],
            summary=_summary(),
        )

    mock_cv.assert_not_called()
    assert "_cross_validation" not in result or result.get("_cross_validation") is None


@pytest.mark.asyncio
async def test_analyze_cross_validation_not_triggered_for_low_severity():
    """restart_pod with low severity → cross-validation NOT run."""
    from app.llm.client import analyze

    llm_result = _make_llm_result("restart_pod")  # destructive but low severity
    with (
        patch("app.llm.client._analyze_with_model", AsyncMock(return_value=llm_result)),
        patch("app.llm.client._cross_validate", AsyncMock()) as mock_cv,
    ):
        result = await analyze(
            service="svc",
            environment="dev",
            error_type="runtime_crash",
            severity="low",   # <-- not high/critical
            key_events=["error"],
            summary=_summary(),
        )

    mock_cv.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_cross_validation_triggered_for_destructive_high():
    """restart_pod + high severity → cross-validation IS run."""
    from app.llm.client import analyze

    llm_result = _make_llm_result("restart_pod")
    with (
        patch("app.llm.client._analyze_with_model", AsyncMock(return_value=llm_result)),
        patch("app.llm.client._cross_validate", AsyncMock(return_value={
            **llm_result, "_cross_validation": {"agreed": True, "skipped": False},
        })) as mock_cv,
    ):
        result = await analyze(
            service="svc",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            key_events=["crash"],
            summary=_summary(),
        )

    mock_cv.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_cross_validation_triggered_for_destructive_critical():
    """rollback + critical severity → cross-validation IS run."""
    from app.llm.client import analyze

    llm_result = _make_llm_result("rollback")
    with (
        patch("app.llm.client._analyze_with_model", AsyncMock(return_value=llm_result)),
        patch("app.llm.client._cross_validate", AsyncMock(return_value={
            **llm_result, "_cross_validation": {"agreed": True, "skipped": False},
        })) as mock_cv,
    ):
        result = await analyze(
            service="svc",
            environment="prod",
            error_type="runtime_crash",
            severity="critical",
            key_events=["crash"],
            summary=_summary(),
        )

    mock_cv.assert_called_once()


# ── run_analysis integration: cross_validation in AnalysisResult ───────────────

_SAMPLE_LOGS = [
    {"message": "pod OOMKilled", "level": "ERROR",
     "@timestamp": "2026-05-07T12:00:00Z", "endpoint": "/api"},
] * 5

_MOCK_ES_SEARCH = {
    "hits": {"hits": [{"_source": l} for l in _SAMPLE_LOGS]},
    "aggregations": {},
}


def _make_es_client():
    mock = AsyncMock()
    mock.search = AsyncMock(return_value=_MOCK_ES_SEARCH)
    mock.count  = AsyncMock(return_value={"count": 0})
    mock.index  = AsyncMock(return_value={"_id": "test-id"})
    mock.update = AsyncMock(return_value={"result": "updated"})
    mock.get    = AsyncMock(return_value={"_source": {}, "found": True})
    return mock


@pytest.mark.asyncio
async def test_run_analysis_cross_validation_field_present():
    """AnalysisResult.cross_validation is populated when LLM returns it."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    cv_data = {"agreed": True, "primary_model": "gemma", "secondary_model": "claude", "skipped": False}
    llm_result = {
        "root_causes": [{"cause": "OOMKilled", "confidence": 0.9}],
        "suggestion": "restart the pod",
        "proposed_action": {"type": "notify", "target": "sample-app"},
        "_cross_validation": cv_data,
    }
    es = _make_es_client()

    with (
        patch("app.services.elk_service.get_client", return_value=es),
        patch("app.services.memory_store.get_client", return_value=es),
        patch("app.core.agent.analyze", AsyncMock(return_value=llm_result)),
        patch("app.core.agent.safety_validate", AsyncMock(
            return_value=MagicMock(allowed=True, action="notify", reason="ok", checks={})
        )),
        patch("app.core.agent.compute_anomaly_score", AsyncMock(return_value=0.0)),
        patch("app.core.agent.record_analysis", AsyncMock(return_value="test-id")),
        patch("app.core.agent.find_recent_incidents_for_chain", AsyncMock(return_value=[])),
        patch("app.core.agent.link_incident_to_chain", AsyncMock()),
    ):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    assert result.cross_validation is not None
    assert result.cross_validation["agreed"] is True
    assert result.cross_validation["primary_model"] == "gemma"


@pytest.mark.asyncio
async def test_run_analysis_cross_validation_none_when_not_present():
    """AnalysisResult.cross_validation is None when LLM result has no _cross_validation."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    llm_result = {
        "root_causes": [{"cause": "connection refused", "confidence": 0.7}],
        "suggestion": "check db",
        "proposed_action": {"type": "notify", "target": "sample-app"},
        # no _cross_validation key
    }
    es = _make_es_client()

    with (
        patch("app.services.elk_service.get_client", return_value=es),
        patch("app.services.memory_store.get_client", return_value=es),
        patch("app.core.agent.analyze", AsyncMock(return_value=llm_result)),
        patch("app.core.agent.safety_validate", AsyncMock(
            return_value=MagicMock(allowed=True, action="notify", reason="ok", checks={})
        )),
        patch("app.core.agent.compute_anomaly_score", AsyncMock(return_value=0.0)),
        patch("app.core.agent.record_analysis", AsyncMock(return_value="test-id")),
        patch("app.core.agent.find_recent_incidents_for_chain", AsyncMock(return_value=[])),
        patch("app.core.agent.link_incident_to_chain", AsyncMock()),
    ):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    assert result.cross_validation is None


@pytest.mark.asyncio
async def test_run_analysis_anomaly_score_passed_to_safety():
    """compute_anomaly_score result should be forwarded to safety_validate."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    llm_result = {
        "root_causes": [{"cause": "crash", "confidence": 0.9}],
        "suggestion": "restart",
        "proposed_action": {"type": "notify", "target": "sample-app"},
    }
    es = _make_es_client()
    safety_mock = AsyncMock(
        return_value=MagicMock(allowed=True, action="notify", reason="ok", checks={})
    )

    with (
        patch("app.services.elk_service.get_client", return_value=es),
        patch("app.services.memory_store.get_client", return_value=es),
        patch("app.core.agent.analyze", AsyncMock(return_value=llm_result)),
        patch("app.core.agent.safety_validate", safety_mock),
        patch("app.core.agent.compute_anomaly_score", AsyncMock(return_value=2.75)),
        patch("app.core.agent.record_analysis", AsyncMock(return_value="test-id")),
        patch("app.core.agent.find_recent_incidents_for_chain", AsyncMock(return_value=[])),
        patch("app.core.agent.link_incident_to_chain", AsyncMock()),
    ):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    # Verify anomaly_score was passed to safety_validate
    call_kwargs = safety_mock.call_args.kwargs
    assert call_kwargs.get("anomaly_score") == pytest.approx(2.75)

    # Verify it's in the result
    assert result.anomaly_score == pytest.approx(2.75)


@pytest.mark.asyncio
async def test_run_analysis_anomaly_score_default_zero_on_error():
    """If compute_anomaly_score raises, anomaly_score defaults to 0.0."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    llm_result = {
        "root_causes": [{"cause": "crash", "confidence": 0.9}],
        "suggestion": "restart",
        "proposed_action": {"type": "notify", "target": "sample-app"},
    }
    es = _make_es_client()
    safety_mock = AsyncMock(
        return_value=MagicMock(allowed=True, action="notify", reason="ok", checks={})
    )

    with (
        patch("app.services.elk_service.get_client", return_value=es),
        patch("app.services.memory_store.get_client", return_value=es),
        patch("app.core.agent.analyze", AsyncMock(return_value=llm_result)),
        patch("app.core.agent.safety_validate", safety_mock),
        patch("app.core.agent.compute_anomaly_score",
              AsyncMock(side_effect=Exception("ES down"))),
        patch("app.core.agent.record_analysis", AsyncMock(return_value="test-id")),
        patch("app.core.agent.find_recent_incidents_for_chain", AsyncMock(return_value=[])),
        patch("app.core.agent.link_incident_to_chain", AsyncMock()),
    ):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    # Should complete without raising; anomaly_score defaults to -1.0 (bypass sentinel)
    assert result.anomaly_score == -1.0
    # safety_validate should still be called with anomaly_score=-1.0 (bypass gate)
    call_kwargs = safety_mock.call_args.kwargs
    assert call_kwargs.get("anomaly_score") == -1.0


# ── Schema: new fields present with defaults ──────────────────────────────────

def test_analysis_result_has_anomaly_score_field():
    from app.models.schemas import AnalysisResult

    fields = AnalysisResult.model_fields
    assert "anomaly_score" in fields
    assert "cross_validation" in fields


def test_analysis_result_anomaly_score_defaults_to_zero():
    from app.models.schemas import AnalysisResult

    result = AnalysisResult(
        service="svc", environment="dev",
        root_cause="crash", root_causes=[{"cause": "crash", "confidence": 0.9}],
        suggestion="restart",
        confidence_hint="high", confidence_score=7,
        parsed_log={"error_type": "runtime_crash", "severity": "high",
                    "key_events": [], "summary": "test"},
        raw_evidence=[], log_summary={},
    )
    assert result.anomaly_score == 0.0
    assert result.cross_validation is None

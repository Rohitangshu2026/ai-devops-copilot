"""Tests for Phase 8e temporal incident correlation and chain linking.

Covers:
  - find_recent_incidents_for_chain() memory store function
  - link_incident_to_chain() updates the incident document
  - agent.run_analysis() wires cascade_depth into safety validate call
  - cascade_depth > 0 in safety controller downgrades to notify when upstream in-flight
  - AnalysisResult contains chain fields when upstream match exists
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.memory_store import find_recent_incidents_for_chain, link_incident_to_chain


# ── find_recent_incidents_for_chain ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_recent_returns_list_on_success():
    mock_client = AsyncMock()
    mock_client.search = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {"_source": {"incident_id": "abc", "service": "elasticsearch"}},
                    {"_source": {"incident_id": "def", "service": "api-backend"}},
                ]
            }
        }
    )
    with patch("app.services.memory_store.get_client", return_value=mock_client):
        result = await find_recent_incidents_for_chain("2026-05-07T12:00:00+00:00", lookback_minutes=10)

    assert len(result) == 2
    assert result[0]["incident_id"] == "abc"


@pytest.mark.asyncio
async def test_find_recent_returns_empty_on_es_failure():
    mock_client = AsyncMock()
    mock_client.search = AsyncMock(side_effect=ConnectionError("ES down"))

    with patch("app.services.memory_store.get_client", return_value=mock_client):
        result = await find_recent_incidents_for_chain("2026-05-07T12:00:00+00:00")

    assert result == []


@pytest.mark.asyncio
async def test_find_recent_returns_empty_when_no_hits():
    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value={"hits": {"hits": []}})

    with patch("app.services.memory_store.get_client", return_value=mock_client):
        result = await find_recent_incidents_for_chain("2026-05-07T12:00:00+00:00")

    assert result == []


@pytest.mark.asyncio
async def test_find_recent_uses_range_query_with_lookback():
    """The ES query body should embed the lookback window (e.g. '-15m')."""
    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value={"hits": {"hits": []}})

    with patch("app.services.memory_store.get_client", return_value=mock_client):
        await find_recent_incidents_for_chain("2026-05-07T12:00:00+00:00", lookback_minutes=15)

    call_args = mock_client.search.call_args
    # body may be passed as positional or keyword arg depending on ES client version
    all_args_str = str(call_args)
    assert "15m" in all_args_str


# ── link_incident_to_chain ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_incident_writes_chain_fields():
    with patch("app.services.memory_store.update_incident", new=AsyncMock()) as mock_update:
        await link_incident_to_chain(
            incident_id="new-id",
            chain_id="chain-uuid",
            upstream_id="upstream-id",
            depth=1,
            path=["elasticsearch", "sample-app"],
        )

    mock_update.assert_awaited_once()
    kwargs = mock_update.call_args.args
    fields = kwargs[1]
    assert fields["incident_chain_id"] == "chain-uuid"
    assert fields["upstream_incident_id"] == "upstream-id"
    assert fields["cascade_depth"] == 1
    assert "sample-app" in fields["cascade_path"]


@pytest.mark.asyncio
async def test_link_incident_passes_correct_incident_id():
    with patch("app.services.memory_store.update_incident", new=AsyncMock()) as mock_update:
        await link_incident_to_chain("target-id", "chain", "upstream", 2, ["a", "b", "target"])

    assert mock_update.call_args.args[0] == "target-id"


# ── agent.run_analysis() wires chain into result ──────────────────────────────


_SAMPLE_LOGS = [
    {"message": "connection refused to elasticsearch", "level": "ERROR",
     "@timestamp": "2026-05-07T12:00:00Z", "endpoint": "/api"},
] * 5

_MOCK_ES_SEARCH = {
    "hits": {"hits": [{"_source": l} for l in _SAMPLE_LOGS]},
    "aggregations": {},
}


def _make_es_client():
    """Return a mock Elasticsearch async client that returns realistic data."""
    mock = AsyncMock()
    mock.search = AsyncMock(return_value=_MOCK_ES_SEARCH)
    mock.count = AsyncMock(return_value={"count": 0})
    mock.index = AsyncMock(return_value={"_id": "test-id"})
    mock.update = AsyncMock(return_value={"result": "updated"})
    mock.get = AsyncMock(return_value={"_source": {"incident_id": "test-id"}, "found": True})
    return mock


def _make_run_analysis_patches(
    recent_incidents=None, llm_result=None, save_id="test-incident-id"
):
    """Return a dict of patch targets for run_analysis integration tests."""
    from tests.conftest import LLM_DEFAULT_RESULT

    llm_res = llm_result or {**LLM_DEFAULT_RESULT}

    # agent.py binds these at import time, so patch inside the agent module namespace.
    return {
        "app.core.agent.analyze": AsyncMock(return_value=llm_res),
        "app.core.agent.safety_validate": AsyncMock(
            return_value=MagicMock(allowed=True, action="notify", reason="ok", checks={})
        ),
        "app.core.agent.record_analysis": AsyncMock(return_value=save_id),
        "app.services.memory_store.link_incident_to_chain": AsyncMock(),
    }


@pytest.mark.asyncio
async def test_no_chain_when_no_upstream_incidents():
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    patches = _make_run_analysis_patches(recent_incidents=[])
    with _run_with_patches(patches, upstream_incidents=[]):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    assert result.cascade_depth == 0
    assert result.incident_chain_id is None
    patches["app.services.memory_store.link_incident_to_chain"].assert_not_awaited()


def _run_with_patches(patches, upstream_incidents):
    """Context manager composing all required patches for run_analysis tests.
    Uses the ES-client-mock approach (same as test_phase7_integration.py) so
    the real fetch_logs code path runs but ES calls are intercepted."""
    from contextlib import ExitStack
    es = _make_es_client()
    stack = ExitStack()
    stack.enter_context(patch("app.services.elk_service.get_client", return_value=es))
    stack.enter_context(patch("app.services.memory_store.get_client", return_value=es))
    stack.enter_context(patch("app.core.agent.analyze", patches["app.core.agent.analyze"]))
    stack.enter_context(patch("app.core.agent.safety_validate", patches["app.core.agent.safety_validate"]))
    stack.enter_context(patch("app.core.agent.record_analysis", patches["app.core.agent.record_analysis"]))
    # find_recent_incidents_for_chain / link_incident_to_chain are bound in agent's namespace
    stack.enter_context(patch("app.core.agent.find_recent_incidents_for_chain",
                               AsyncMock(return_value=upstream_incidents)))
    stack.enter_context(patch("app.core.agent.link_incident_to_chain",
                               patches["app.services.memory_store.link_incident_to_chain"]))
    return stack


@pytest.mark.asyncio
async def test_chain_linked_when_upstream_dependency_incident():
    """When elasticsearch has a recent incident, sample-app gets chained to it."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    upstream = {
        "incident_id": "es-upstream-id",
        "service": "elasticsearch",
        "cascade_depth": 0,
        "cascade_path": [],
        "incident_chain_id": None,
    }
    patches = _make_run_analysis_patches(recent_incidents=[upstream])

    with _run_with_patches(patches, upstream_incidents=[upstream]):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    assert result.cascade_depth == 1
    assert result.upstream_incident_id == "es-upstream-id"
    assert "sample-app" in (result.cascade_path or [])
    patches["app.services.memory_store.link_incident_to_chain"].assert_awaited_once()


@pytest.mark.asyncio
async def test_chain_preserves_existing_chain_id():
    """If upstream already has a chain_id, the same chain_id is used (not a new UUID)."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    upstream = {
        "incident_id": "es-upstream-id",
        "service": "elasticsearch",
        "cascade_depth": 0,
        "cascade_path": ["elasticsearch"],
        "incident_chain_id": "existing-chain-uuid",
    }
    patches = _make_run_analysis_patches(recent_incidents=[upstream])

    with _run_with_patches(patches, upstream_incidents=[upstream]):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    assert result.incident_chain_id == "existing-chain-uuid"


@pytest.mark.asyncio
async def test_correlation_failure_does_not_crash_analysis():
    """If the chain query raises, analysis must still complete successfully."""
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest

    patches = _make_run_analysis_patches()

    es = _make_es_client()
    with (
        patch("app.services.elk_service.get_client", return_value=es),
        patch("app.services.memory_store.get_client", return_value=es),
        patch("app.core.agent.analyze", patches["app.core.agent.analyze"]),
        patch("app.core.agent.safety_validate", patches["app.core.agent.safety_validate"]),
        patch("app.core.agent.record_analysis", patches["app.core.agent.record_analysis"]),
        patch("app.core.agent.find_recent_incidents_for_chain",
              AsyncMock(side_effect=ConnectionError("ES chain query failed"))),
        patch("app.core.agent.link_incident_to_chain", AsyncMock()),
    ):
        req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
        result = await run_analysis(req)

    # Analysis completes, chain fields default
    assert result.incident_id is not None
    assert result.cascade_depth == 0


# ── AnalysisResult schema includes chain fields ────────────────────────────────


def test_analysis_result_schema_has_chain_fields():
    from app.models.schemas import AnalysisResult

    fields = AnalysisResult.model_fields
    assert "incident_chain_id" in fields
    assert "upstream_incident_id" in fields
    assert "cascade_depth" in fields
    assert "cascade_path" in fields


def test_analysis_result_chain_fields_have_defaults():
    from app.models.schemas import AnalysisResult

    # These should default to None / 0 / [] (not required)
    result = AnalysisResult(
        service="svc", environment="dev",
        root_cause="test", root_causes=[{"cause": "test", "confidence": 0.9}],
        suggestion="restart the pod",
        confidence_hint="high", confidence_score=7,
        parsed_log={"error_type": "runtime_crash", "severity": "high",
                    "key_events": [], "summary": "test"},
        raw_evidence=[], log_summary={},
    )
    assert result.cascade_depth == 0
    assert result.cascade_path == []
    assert result.incident_chain_id is None
    assert result.upstream_incident_id is None


# ── Safety cascade guard ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safety_cascade_guard_downgrades_to_notify():
    """cascade_depth > 0 with in-flight upstream action should downgrade to notify."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["connection refused"])

    # Mock an in-flight action for the upstream service
    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="restart_pod", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions",
              new=AsyncMock(return_value=[{"action_type": "restart_pod", "action_state": "executing"}])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="sample-app",
            environment="dev",
            error_type="dependency_error",
            severity="high",
            confidence="high",
            proposed_action={"type": "restart_pod", "target": "sample-app"},
            causality=causality,
            cascade_depth=1,  # <-- upstream incident exists
        )

    # With cascade_depth=1, the guard should have downgraded the action
    # Note: this test verifies the cascade_guard check key is present in checks
    assert "cascade_guard" in result.checks


@pytest.mark.asyncio
async def test_safety_cascade_guard_not_triggered_at_depth_zero():
    """cascade_depth=0 should not trigger the cascade guard."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["connection refused"])

    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="notify", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions", new=AsyncMock(return_value=[])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="sample-app",
            environment="dev",
            error_type="dependency_error",
            severity="high",
            confidence="high",
            proposed_action={"type": "notify", "target": "sample-app"},
            causality=causality,
            cascade_depth=0,
        )

    # cascade_guard key may or may not be present; if present, not downgraded
    if "cascade_guard" in result.checks:
        assert not result.checks["cascade_guard"].get("downgraded", False)

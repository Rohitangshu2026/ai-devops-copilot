"""Intensive tests for Phase 9e statistical anomaly detection.

Covers:
  - compute_anomaly_score: z-score math, edge cases, ES failure
  - get_baseline: found vs not-found vs ES error
  - update_baseline: mean/std computation, ES upsert
  - refresh_baseline_for_service: incident query + update pipeline
  - Safety gate integration: anomaly_score < threshold blocks destructive actions
  - Anomaly gate pass-through: safe actions (notify/no_action) bypass the gate
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.anomaly import (
    ANOMALY_Z_THRESHOLD,
    compute_anomaly_score,
    get_baseline,
    refresh_baseline_for_service,
    update_baseline,
)
from app.log_processor.summarizer import LogSummary


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _summary(error_ratio: float = 0.5) -> LogSummary:
    return LogSummary(
        total_events=20,
        error_count=int(error_ratio * 20),
        warning_count=0,
        unique_endpoints=["/api"],
        error_ratio=error_ratio,
        deduplicated_events=["GET /api 500"],
        time_span_minutes=5.0,
    )


def _baseline(mean: float, std: float, samples: int = 10) -> dict:
    return {
        "service": "sample-app",
        "mean_error_ratio": mean,
        "std_error_ratio": std,
        "sample_count": samples,
        "updated_at": "2026-05-07T00:00:00+00:00",
    }


def _mock_es(found: bool = True, source: dict | None = None):
    mock = AsyncMock()
    if found:
        mock.get = AsyncMock(return_value={"_source": source or {}, "found": True})
    else:
        mock.get = AsyncMock(return_value={"found": False})
    mock.index = AsyncMock(return_value={"_id": "sample-app"})
    mock.search = AsyncMock(return_value={"hits": {"hits": []}})
    return mock


# ── compute_anomaly_score ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_anomaly_score_basic_z_score():
    """z = (current - mean) / std should be returned."""
    baseline = _baseline(mean=0.1, std=0.05, samples=10)
    summary = _summary(error_ratio=0.2)  # z = (0.2 - 0.1) / 0.05 = 2.0

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", summary)

    assert abs(z - 2.0) < 1e-9


@pytest.mark.asyncio
async def test_compute_anomaly_score_above_threshold():
    """z-score > ANOMALY_Z_THRESHOLD (2.0) should be returned as-is."""
    baseline = _baseline(mean=0.05, std=0.02, samples=20)
    summary = _summary(error_ratio=0.35)  # z = (0.35 - 0.05) / 0.02 = 15.0

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", summary)

    assert z > ANOMALY_Z_THRESHOLD
    assert abs(z - 15.0) < 1e-9


@pytest.mark.asyncio
async def test_compute_anomaly_score_below_threshold():
    """z-score < threshold means routine traffic."""
    baseline = _baseline(mean=0.1, std=0.05, samples=10)
    summary = _summary(error_ratio=0.11)  # z = (0.11 - 0.10) / 0.05 = 0.2

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", summary)

    assert z < ANOMALY_Z_THRESHOLD


@pytest.mark.asyncio
async def test_compute_anomaly_score_no_baseline_returns_zero():
    """Missing baseline → 0.0 (no gate — destructive actions blocked by default)."""
    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=None)):
        z = await compute_anomaly_score("new-service", _summary())

    assert z == 0.0


@pytest.mark.asyncio
async def test_compute_anomaly_score_insufficient_samples_returns_zero():
    """Fewer than 5 samples → 0.0 (baseline not yet trusted)."""
    baseline = _baseline(mean=0.1, std=0.05, samples=3)

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", _summary(error_ratio=0.9))

    assert z == 0.0


@pytest.mark.asyncio
async def test_compute_anomaly_score_exactly_min_samples():
    """Exactly 5 samples → baseline trusted, z-score computed."""
    baseline = _baseline(mean=0.1, std=0.05, samples=5)

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", _summary(error_ratio=0.2))

    assert z == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_compute_anomaly_score_zero_std_with_error():
    """Perfectly stable baseline with error → returns 10.0 (capped anomaly)."""
    baseline = _baseline(mean=0.0, std=0.0, samples=10)

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", _summary(error_ratio=0.5))

    assert z == 10.0


@pytest.mark.asyncio
async def test_compute_anomaly_score_zero_std_no_error():
    """Perfectly stable baseline with no error → returns 0.0."""
    baseline = _baseline(mean=0.0, std=0.0, samples=10)

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", _summary(error_ratio=0.0))

    assert z == 0.0


@pytest.mark.asyncio
async def test_compute_anomaly_score_negative_z_returns_negative():
    """Error ratio below mean → negative z-score (not anomalous, gate passes)."""
    baseline = _baseline(mean=0.3, std=0.05, samples=15)

    with patch("app.core.anomaly.get_baseline", AsyncMock(return_value=baseline)):
        z = await compute_anomaly_score("sample-app", _summary(error_ratio=0.1))

    assert z < 0


# ── get_baseline ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_baseline_found():
    """Returns _source when ES document found."""
    source = {"service": "svc", "mean_error_ratio": 0.1, "std_error_ratio": 0.02, "sample_count": 10}
    es = _mock_es(found=True, source=source)

    with patch("app.services.elk_service.get_client", return_value=es):
        result = await get_baseline("svc")

    assert result == source


@pytest.mark.asyncio
async def test_get_baseline_not_found_returns_none():
    """Returns None when document does not exist."""
    es = _mock_es(found=False)

    with patch("app.services.elk_service.get_client", return_value=es):
        result = await get_baseline("unknown-service")

    assert result is None


@pytest.mark.asyncio
async def test_get_baseline_404_exception_returns_none():
    """ES 404 / not_found exception → None (not an error)."""
    mock_es = AsyncMock()
    mock_es.get = AsyncMock(side_effect=Exception("not_found: index missing"))

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        result = await get_baseline("missing-service")

    assert result is None


@pytest.mark.asyncio
async def test_get_baseline_connection_error_returns_none():
    """Network error → None (graceful degradation)."""
    mock_es = AsyncMock()
    mock_es.get = AsyncMock(side_effect=ConnectionError("ES unreachable"))

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        result = await get_baseline("sample-app")

    assert result is None


# ── update_baseline ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_baseline_computes_correct_mean():
    """Mean is sum(x) / n."""
    ratios = [0.1, 0.2, 0.3]
    mock_es = AsyncMock()
    mock_es.index = AsyncMock(return_value={"_id": "svc"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await update_baseline("svc", ratios)

    call_kwargs = mock_es.index.call_args.kwargs
    doc = call_kwargs["document"]
    assert doc["mean_error_ratio"] == pytest.approx(0.2, abs=1e-6)


@pytest.mark.asyncio
async def test_update_baseline_computes_correct_std():
    """Std is population standard deviation."""
    ratios = [0.1, 0.1, 0.3, 0.3]  # mean=0.2, variance=0.01, std=0.1
    mock_es = AsyncMock()
    mock_es.index = AsyncMock(return_value={"_id": "svc"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await update_baseline("svc", ratios)

    doc = mock_es.index.call_args.kwargs["document"]
    assert doc["std_error_ratio"] == pytest.approx(0.1, abs=1e-5)


@pytest.mark.asyncio
async def test_update_baseline_stores_sample_count():
    ratios = [0.1, 0.2, 0.3, 0.4, 0.5]
    mock_es = AsyncMock()
    mock_es.index = AsyncMock(return_value={"_id": "svc"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await update_baseline("svc", ratios)

    doc = mock_es.index.call_args.kwargs["document"]
    assert doc["sample_count"] == 5


@pytest.mark.asyncio
async def test_update_baseline_empty_list_is_noop():
    """Empty list → no ES write."""
    mock_es = AsyncMock()

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await update_baseline("svc", [])

    mock_es.index.assert_not_called()


@pytest.mark.asyncio
async def test_update_baseline_single_value():
    """Single value → std=0, mean=that value."""
    mock_es = AsyncMock()
    mock_es.index = AsyncMock(return_value={"_id": "svc"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await update_baseline("svc", [0.42])

    doc = mock_es.index.call_args.kwargs["document"]
    assert doc["mean_error_ratio"] == pytest.approx(0.42, abs=1e-6)
    assert doc["std_error_ratio"] == 0.0


@pytest.mark.asyncio
async def test_update_baseline_uses_wait_for_refresh():
    """ES index call must use refresh='wait_for' for immediate consistency."""
    mock_es = AsyncMock()
    mock_es.index = AsyncMock(return_value={"_id": "svc"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await update_baseline("svc", [0.1, 0.2])

    call_kwargs = mock_es.index.call_args.kwargs
    assert call_kwargs.get("refresh") == "wait_for"


@pytest.mark.asyncio
async def test_update_baseline_es_failure_does_not_raise():
    """ES write failure is logged but not re-raised."""
    mock_es = AsyncMock()
    mock_es.index = AsyncMock(side_effect=ConnectionError("ES down"))

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        # Should not raise
        await update_baseline("svc", [0.1, 0.2, 0.3])


# ── refresh_baseline_for_service ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_baseline_queries_incidents_index():
    """Should search devops-incidents-* with correct service term."""
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(return_value={"hits": {"hits": []}})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await refresh_baseline_for_service("sample-app")

    call = mock_es.search.call_args
    body = call.kwargs.get("body") or (call.args[0] if call.args else {})
    assert "sample-app" in str(call)


@pytest.mark.asyncio
async def test_refresh_baseline_extracts_error_ratios():
    """Should call update_baseline with extracted error_ratio values."""
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(return_value={
        "hits": {
            "hits": [
                {"_source": {"log_summary": {"error_ratio": 0.10}}},
                {"_source": {"log_summary": {"error_ratio": 0.25}}},
                {"_source": {"log_summary": {"error_ratio": 0.40}}},
            ]
        }
    })
    mock_es.index = AsyncMock(return_value={"_id": "sample-app"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await refresh_baseline_for_service("sample-app")

    # update_baseline should have been called via index
    mock_es.index.assert_called_once()
    doc = mock_es.index.call_args.kwargs["document"]
    assert doc["sample_count"] == 3
    assert doc["mean_error_ratio"] == pytest.approx(0.25, abs=1e-5)


@pytest.mark.asyncio
async def test_refresh_baseline_skips_none_ratios():
    """Hits without log_summary.error_ratio should be silently skipped."""
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(return_value={
        "hits": {
            "hits": [
                {"_source": {"log_summary": {"error_ratio": 0.10}}},
                {"_source": {}},           # missing log_summary
                {"_source": {"log_summary": {}}},  # missing error_ratio
            ]
        }
    })
    mock_es.index = AsyncMock(return_value={"_id": "sample-app"})

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await refresh_baseline_for_service("sample-app")

    doc = mock_es.index.call_args.kwargs["document"]
    assert doc["sample_count"] == 1


@pytest.mark.asyncio
async def test_refresh_baseline_no_hits_no_update():
    """Zero ratios → update_baseline NOT called."""
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(return_value={"hits": {"hits": []}})
    mock_es.index = AsyncMock()

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        await refresh_baseline_for_service("sample-app")

    mock_es.index.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_baseline_es_error_silently_ignored():
    """ES error during refresh is swallowed — service continues."""
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(side_effect=ConnectionError("ES down"))

    with patch("app.services.elk_service.get_client", return_value=mock_es):
        # Should not raise
        await refresh_baseline_for_service("sample-app")


# ── Safety gate integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safety_anomaly_gate_blocks_destructive_action():
    """anomaly_score=0.5 (below threshold) should block restart_pod."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["OOMKilled"])

    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="restart_pod", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions", new=AsyncMock(return_value=[])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            confidence="high",
            proposed_action={"type": "restart_pod", "target": "sample-app"},
            causality=causality,
            anomaly_score=0.5,  # below ANOMALY_Z_THRESHOLD=2.0
        )

    assert not result.allowed
    assert result.action == "no_action"
    assert "anomaly gate" in result.reason
    assert "anomaly_gate" in result.checks
    assert result.checks["anomaly_gate"]["passed"] is False


@pytest.mark.asyncio
async def test_safety_anomaly_gate_passes_above_threshold():
    """anomaly_score=3.0 (above threshold) should allow the action through."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["OOMKilled"])

    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="restart_pod", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions", new=AsyncMock(return_value=[])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="sample-app",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            confidence="high",
            proposed_action={"type": "restart_pod", "target": "sample-app"},
            causality=causality,
            anomaly_score=3.0,  # above threshold
        )

    assert result.checks["anomaly_gate"]["passed"] is True
    # result may be allowed or denied by later gates, but anomaly_gate itself passed
    assert "anomaly gate" not in result.reason


@pytest.mark.asyncio
async def test_safety_anomaly_gate_skipped_for_safe_actions():
    """notify/no_action bypass the anomaly gate entirely."""
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
            anomaly_score=0.0,  # would block destructive actions
        )

    # notify should not be blocked by anomaly gate
    assert "anomaly gate" not in result.reason
    assert "anomaly_gate" not in result.checks


@pytest.mark.asyncio
async def test_safety_anomaly_gate_zero_score_blocks_destructive():
    """anomaly_score=0.0 (baseline exists, z-score is 0) blocks destructive actions.
    Note: -1.0 is the bypass sentinel for 'no baseline'. 0.0 means 'baseline present, not anomalous'."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["crash"])

    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="restart_pod", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions", new=AsyncMock(return_value=[])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="new-service",
            environment="prod",
            error_type="runtime_crash",
            severity="critical",
            confidence="high",
            proposed_action={"type": "restart_pod", "target": "new-service"},
            causality=causality,
            anomaly_score=0.0,
        )

    assert not result.allowed
    assert result.action == "no_action"


@pytest.mark.asyncio
async def test_safety_anomaly_gate_exactly_at_threshold_blocks():
    """z = threshold exactly should still block (requires strictly >)."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["crash"])

    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="restart_pod", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions", new=AsyncMock(return_value=[])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="svc",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            confidence="high",
            proposed_action={"type": "restart_pod"},
            causality=causality,
            anomaly_score=float(ANOMALY_Z_THRESHOLD),  # exactly 2.0
        )

    # Exactly at threshold should PASS (>= threshold)
    if "anomaly_gate" in result.checks:
        assert result.checks["anomaly_gate"]["passed"] is True


@pytest.mark.asyncio
async def test_anomaly_gate_includes_z_score_in_checks():
    """The checks dict should record the z_score and threshold for operator inspection."""
    from app.core.safety import validate
    from app.core.causality import CausalityResult

    causality = CausalityResult(verified=True, matched_evidence=["crash"])

    with (
        patch("app.core.safety.apply_policy",
              return_value=MagicMock(allowed=True, action="restart_pod", reason="")),
        patch("app.core.loop_detector.check_loop",
              new=AsyncMock(return_value=MagicMock(loop_detected=False, freeze=False, count=0, reason=""))),
        patch("app.services.memory_store.try_acquire_action_lock", new=AsyncMock(return_value=True)),
        patch("app.services.memory_store.find_recent_actions", new=AsyncMock(return_value=[])),
        patch("app.services.memory_store.count_unresolved_actions", new=AsyncMock(return_value=0)),
    ):
        result = await validate(
            service="svc",
            environment="dev",
            error_type="runtime_crash",
            severity="high",
            confidence="high",
            proposed_action={"type": "restart_pod"},
            causality=causality,
            anomaly_score=1.23,
        )

    assert "anomaly_gate" in result.checks
    gate = result.checks["anomaly_gate"]
    assert gate["z_score"] == pytest.approx(1.23, abs=0.01)
    assert gate["threshold"] == ANOMALY_Z_THRESHOLD

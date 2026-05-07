"""Tests for the Phase 8a memory-aware confidence boost.

Covers the historical-match path in score_confidence() that queries
Elasticsearch for similar past incidents.  ES is mocked throughout.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.confidence import score_confidence
from app.log_processor.summarizer import LogSummary


def _summary(error_count=5, total_events=15, error_ratio=0.33):
    return LogSummary(
        total_events=total_events,
        error_count=error_count,
        warning_count=0,
        unique_endpoints=["/error"],
        error_ratio=error_ratio,
        deduplicated_events=["GET /error 500"],
        time_span_minutes=5.0,
    )


# ── Helper patches ─────────────────────────────────────────────────────────────


def _patch_similar(incidents: list[dict]):
    """Patch find_similar_incidents where it is defined (memory_store).
    confidence.py imports it inside the function body so we patch the source module."""
    return patch(
        "app.services.memory_store.find_similar_incidents",
        new=AsyncMock(return_value=incidents),
    )


# ── Memory boost: applied ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_historical_boost_applied_when_two_resolved():
    """Score gets +2 when ≥2 of the 3 similar incidents were resolved."""
    incidents = [
        {"outcome": "resolved"},
        {"outcome": "resolved"},
        {"outcome": "unresolved"},
    ]
    s = _summary()
    with _patch_similar(incidents):
        _, score_with, breakdown_with = await score_confidence(
            s, "runtime_crash", "high", service="sample-app"
        )
        _, score_without, _ = await score_confidence(
            s, "runtime_crash", "high", service=""
        )

    assert score_with - score_without == 2
    assert any("historical_match" in b for b in breakdown_with)


@pytest.mark.asyncio
async def test_historical_boost_applied_when_all_three_resolved():
    incidents = [{"outcome": "resolved"}] * 3
    s = _summary()
    with _patch_similar(incidents):
        _, score_with, breakdown = await score_confidence(
            s, "runtime_crash", "high", service="svc"
        )
    _, score_without, _ = await score_confidence(s, "runtime_crash", "high", service="")
    assert score_with - score_without == 2
    assert any("3 of 3" in b for b in breakdown)


@pytest.mark.asyncio
async def test_historical_boost_breakdown_mentions_resolved_count():
    incidents = [{"outcome": "resolved"}, {"outcome": "resolved"}, {"outcome": "unresolved"}]
    s = _summary()
    with _patch_similar(incidents):
        _, _, breakdown = await score_confidence(
            s, "dependency_error", "high", service="sample-app"
        )
    text = " ".join(breakdown)
    assert "2 of 3" in text
    assert "historical_match" in text


# ── Memory boost: NOT applied ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_boost_when_only_one_resolved():
    incidents = [
        {"outcome": "resolved"},
        {"outcome": "unresolved"},
        {"outcome": "unresolved"},
    ]
    s = _summary()
    with _patch_similar(incidents):
        _, score_with, breakdown = await score_confidence(
            s, "runtime_crash", "high", service="sample-app"
        )
    _, score_without, _ = await score_confidence(s, "runtime_crash", "high", service="")
    assert score_with == score_without
    assert not any("historical_match" in b for b in breakdown)


@pytest.mark.asyncio
async def test_no_boost_when_no_history():
    """Empty history list → no boost."""
    s = _summary()
    with _patch_similar([]):
        _, score_with, breakdown = await score_confidence(
            s, "runtime_crash", "high", service="sample-app"
        )
    _, score_without, _ = await score_confidence(s, "runtime_crash", "high", service="")
    assert score_with == score_without
    assert not any("historical_match" in b for b in breakdown)


@pytest.mark.asyncio
async def test_no_boost_without_service_arg():
    """service='' (default) skips the ES query entirely."""
    s = _summary()
    # No patch needed — ES should not be called at all
    _, _, breakdown = await score_confidence(s, "runtime_crash", "critical")
    assert not any("historical_match" in b for b in breakdown)


# ── ES failure is swallowed gracefully ────────────────────────────────────────


@pytest.mark.asyncio
async def test_es_failure_does_not_crash_confidence():
    """If the ES query raises, score_confidence falls back gracefully."""
    s = _summary()
    with patch(
        "app.services.memory_store.find_similar_incidents",
        new=AsyncMock(side_effect=ConnectionError("ES unavailable")),
    ):
        label, score, breakdown = await score_confidence(
            s, "runtime_crash", "high", service="sample-app"
        )

    # base score without boost: +2 type + +2 severity + +2 ratio + +1 count + +1 total + +1 runtime = 9
    assert score == 9
    assert label == "high"
    assert not any("historical_match" in b for b in breakdown)


@pytest.mark.asyncio
async def test_confidence_source_label_not_changed_by_mock():
    """confidence_source is 'signal' or 'memory_boost' — just verify score math."""
    s = _summary()
    with _patch_similar([{"outcome": "resolved"}, {"outcome": "resolved"}]):
        label, score, breakdown = await score_confidence(
            s, "dependency_error", "high", service="sample-app"
        )
    # +2 type + +2 severity + +2 ratio + +1 count + +1 total + +2 boost = 10 → capped? No cap.
    assert score >= 9
    assert label == "high"


# ── Integration: boost pushed label over threshold ────────────────────────────


@pytest.mark.asyncio
async def test_boost_pushes_medium_to_high():
    """A score of 5 (medium) gets +2 boost → 7 (high)."""
    # +2 error_type + +1 count + +2 ratio = 5; severity low (no +2)
    s = _summary(error_count=3, total_events=5, error_ratio=0.15)
    with _patch_similar([{"outcome": "resolved"}, {"outcome": "resolved"}]):
        label, score, _ = await score_confidence(
            s, "dependency_error", "low", service="sample-app"
        )
    assert score == 7
    assert label == "high"


@pytest.mark.asyncio
async def test_boost_pushes_low_to_medium():
    """A score of 2 (low) gets +2 boost → 4 (medium)."""
    s = _summary(error_count=0, total_events=5, error_ratio=0.05)
    # +2 error_type only
    with _patch_similar([{"outcome": "resolved"}, {"outcome": "resolved"}]):
        label, score, _ = await score_confidence(
            s, "dependency_error", "low", service="sample-app"
        )
    assert score == 4
    assert label == "medium"

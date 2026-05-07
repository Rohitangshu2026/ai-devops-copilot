from app.core.confidence import score_confidence
from app.log_processor.summarizer import LogSummary


def _summary(error_count=0, total_events=0, error_ratio=0.0):
    return LogSummary(
        total_events=total_events,
        error_count=error_count,
        warning_count=0,
        unique_endpoints=[],
        error_ratio=error_ratio,
        deduplicated_events=[],
        time_span_minutes=5.0,
    )


def test_low_score_unknown_error_type():
    label, score, breakdown = score_confidence(_summary(1, 5, 0.2), "unknown", "low")
    assert label == "low"
    assert score < 4
    assert isinstance(breakdown, list)


def test_medium_score_with_ratio_and_count():
    # +2 error_ratio>0.10, +1 error_count>=3, +1 total_events>=10
    s = _summary(error_count=5, total_events=15, error_ratio=0.33)
    label, score, _ = score_confidence(s, "unknown", "low")
    assert score == 4
    assert label == "medium"


def test_high_score_all_signals():
    # +2 error_type, +2 severity, +2 ratio, +1 count, +1 total, +1 runtime = 9
    s = _summary(error_count=5, total_events=15, error_ratio=0.33)
    label, score, _ = score_confidence(s, "runtime_crash", "critical")
    assert score == 9
    assert label == "high"


def test_error_type_not_unknown_adds_2():
    s = _summary(0, 0, 0.0)
    _, score_known, _ = score_confidence(s, "dependency_error", "low")
    _, score_unknown, _ = score_confidence(s, "unknown", "low")
    assert score_known - score_unknown == 2


def test_high_severity_adds_2():
    s = _summary(0, 0, 0.0)
    _, score_high, _ = score_confidence(s, "unknown", "high")
    _, score_low, _ = score_confidence(s, "unknown", "low")
    assert score_high - score_low == 2


def test_critical_severity_adds_2():
    s = _summary(0, 0, 0.0)
    _, score, _ = score_confidence(s, "unknown", "critical")
    assert score == 2


def test_error_ratio_above_threshold_adds_2():
    s_above = _summary(error_ratio=0.11)
    s_below = _summary(error_ratio=0.09)
    _, above, _ = score_confidence(s_above, "unknown", "low")
    _, below, _ = score_confidence(s_below, "unknown", "low")
    assert above - below == 2


def test_error_count_threshold():
    s_above = _summary(error_count=3)
    s_below = _summary(error_count=2)
    _, above, _ = score_confidence(s_above, "unknown", "low")
    _, below, _ = score_confidence(s_below, "unknown", "low")
    assert above - below == 1


def test_total_events_threshold():
    s_above = _summary(total_events=10)
    s_below = _summary(total_events=9)
    _, above, _ = score_confidence(s_above, "unknown", "low")
    _, below, _ = score_confidence(s_below, "unknown", "low")
    assert above - below == 1


def test_runtime_crash_adds_1():
    s = _summary()
    _, score_crash, _ = score_confidence(s, "runtime_crash", "low")
    _, score_dep, _ = score_confidence(s, "dependency_error", "low")
    assert score_crash - score_dep == 1


def test_build_failure_adds_1():
    s = _summary()
    _, score_build, _ = score_confidence(s, "build_failure", "low")
    _, score_dep, _ = score_confidence(s, "dependency_error", "low")
    assert score_build - score_dep == 1


def test_score_boundary_medium_at_4():
    # Exactly 4 → medium
    s = _summary(error_count=5, total_events=15, error_ratio=0.33)
    label, score, _ = score_confidence(s, "unknown", "low")
    assert score == 4
    assert label == "medium"


def test_score_boundary_high_at_7():
    # +2 error_type + +2 severity + +2 ratio + +1 count = 7 → high
    s = _summary(error_count=5, total_events=5, error_ratio=0.33)
    label, score, _ = score_confidence(s, "dependency_error", "high")
    assert score == 7
    assert label == "high"


# ── Phase 6g — confidence breakdown tests ────────────────────────────────────


def test_breakdown_is_list_of_strings():
    s = _summary(error_count=5, total_events=15, error_ratio=0.33)
    _, _, breakdown = score_confidence(s, "runtime_crash", "high")
    assert isinstance(breakdown, list)
    assert all(isinstance(b, str) for b in breakdown)


def test_breakdown_entries_match_score():
    """Each breakdown entry starts with '+N' — the sum must equal the score."""
    s = _summary(error_count=5, total_events=15, error_ratio=0.33)
    _, score, breakdown = score_confidence(s, "runtime_crash", "high")
    total = sum(int(b.split()[0]) for b in breakdown)
    assert total == score


def test_breakdown_empty_on_zero_score():
    s = _summary(0, 0, 0.0)
    _, score, breakdown = score_confidence(s, "unknown", "low")
    assert score == 0
    assert breakdown == []


def test_breakdown_mentions_each_signal():
    """Every active signal must produce a labelled entry."""
    s = _summary(error_count=5, total_events=15, error_ratio=0.5)
    _, _, breakdown = score_confidence(s, "runtime_crash", "critical")
    text = " ".join(breakdown)
    assert "error_type=runtime_crash" in text
    assert "severity=critical" in text
    assert "error_ratio" in text
    assert "error_count" in text
    assert "total_events" in text

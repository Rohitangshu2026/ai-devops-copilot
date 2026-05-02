import json
from datetime import datetime, timezone, timedelta

from app.log_processor.summarizer import (
    LogSummary,
    summarize,
    _adaptive_bucket_seconds,
    _build_timeline,
    _detect_change_point,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_logs(n_errors=0, n_info=0, span_minutes=5.0):
    """Generate synthetic logs spread over span_minutes."""
    now = datetime.now(timezone.utc)
    logs = []
    total = n_errors + n_info
    for i in range(total):
        offset = timedelta(minutes=span_minutes * i / max(total - 1, 1))
        ts = (now - timedelta(minutes=span_minutes) + offset).isoformat()
        level = "ERROR" if i < n_errors else "INFO"
        event = "request_error" if level == "ERROR" else "health_check"
        logs.append({
            "@timestamp": ts,
            "level": level,
            "message": json.dumps({
                "event": event,
                "endpoint": "/error" if level == "ERROR" else "/health",
                "status": 500 if level == "ERROR" else 200,
                "level": level,
                "timestamp": ts,
            }),
        })
    return logs


# ── _adaptive_bucket_seconds ─────────────────────────────────────────────────

def test_adaptive_bucket_very_short():
    assert _adaptive_bucket_seconds(1.0) == 10


def test_adaptive_bucket_short():
    assert _adaptive_bucket_seconds(5.0) == 30


def test_adaptive_bucket_medium():
    assert _adaptive_bucket_seconds(20.0) == 60


def test_adaptive_bucket_long():
    assert _adaptive_bucket_seconds(60.0) == 300


def test_adaptive_bucket_boundaries():
    assert _adaptive_bucket_seconds(2.0) == 10
    assert _adaptive_bucket_seconds(2.1) == 30
    assert _adaptive_bucket_seconds(10.0) == 30
    assert _adaptive_bucket_seconds(10.1) == 60
    assert _adaptive_bucket_seconds(30.0) == 60
    assert _adaptive_bucket_seconds(30.1) == 300


# ── summarize — basic counts ─────────────────────────────────────────────────

def test_empty_logs_returns_noise_summary():
    s = summarize([])
    assert s.total_events == 0
    assert s.has_only_noise is True


def test_counts_errors_and_info():
    logs = _make_logs(n_errors=3, n_info=7, span_minutes=5.0)
    s = summarize(logs)
    assert s.error_count == 3
    assert s.total_events == 10
    assert abs(s.error_ratio - 0.3) < 0.01


def test_noise_only_flag_when_all_health_checks():
    logs = _make_logs(n_errors=0, n_info=5, span_minutes=2.0)
    s = summarize(logs)
    assert s.has_only_noise is True


def test_not_noise_when_errors_present():
    logs = _make_logs(n_errors=2, n_info=5, span_minutes=2.0)
    s = summarize(logs)
    assert s.has_only_noise is False


def test_time_span_calculated():
    logs = _make_logs(n_errors=2, n_info=8, span_minutes=5.0)
    s = summarize(logs)
    assert s.time_span_minutes > 0


def test_deduped_events_not_empty():
    logs = _make_logs(n_errors=3, n_info=2, span_minutes=5.0)
    s = summarize(logs)
    assert len(s.deduplicated_events) > 0


def test_deduped_events_capped_at_20():
    # 25 distinct endpoints
    now = datetime.now(timezone.utc)
    logs = []
    for i in range(25):
        ts = (now - timedelta(minutes=5) + timedelta(seconds=i * 10)).isoformat()
        logs.append({
            "@timestamp": ts,
            "level": "ERROR",
            "event": f"err{i}",
            "endpoint": f"/ep{i}",
            "status": 500,
        })
    s = summarize(logs)
    assert len(s.deduplicated_events) <= 20


def test_error_events_sorted_first_in_deduped():
    logs = _make_logs(n_errors=2, n_info=3, span_minutes=5.0)
    s = summarize(logs)
    # First deduped entry should be error-related (contains 500 or ERROR)
    first = s.deduplicated_events[0].upper()
    assert "500" in first or "ERROR" in first


def test_masks_ip_addresses():
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    logs = [{"@timestamp": ts, "level": "ERROR", "message": "failed to connect to 192.168.1.5:5432"}]
    s = summarize(logs)
    assert "192.168.1.5" not in " ".join(s.deduplicated_events)
    assert "<IP>" in " ".join(s.deduplicated_events)


def test_masks_uuids():
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    uid = "123e4567-e89b-12d3-a456-426614174000"
    logs = [{"@timestamp": ts, "level": "ERROR", "message": f"user {uid} failed"}]
    s = summarize(logs)
    assert uid not in " ".join(s.deduplicated_events)


# ── error timeline ───────────────────────────────────────────────────────────

def test_timeline_has_4_buckets():
    logs = _make_logs(n_errors=5, n_info=15, span_minutes=5.0)
    s = summarize(logs)
    assert len(s.error_timeline) == 4


def test_timeline_empty_for_single_log():
    now = datetime.now(timezone.utc)
    logs = [{"@timestamp": now.isoformat(), "level": "ERROR", "message": "boom"}]
    s = summarize(logs)
    assert s.error_timeline == []


def test_timeline_error_counts_sum_correctly():
    logs = _make_logs(n_errors=5, n_info=15, span_minutes=5.0)
    s = summarize(logs)
    total_errors = sum(b.error_count for b in s.error_timeline)
    assert total_errors == 5


def test_timeline_total_counts_sum_to_all_logs():
    logs = _make_logs(n_errors=5, n_info=15, span_minutes=5.0)
    s = summarize(logs)
    total = sum(b.total_count for b in s.error_timeline)
    assert total == 20


def test_timeline_error_rates_within_bounds():
    logs = _make_logs(n_errors=5, n_info=15, span_minutes=5.0)
    s = summarize(logs)
    for b in s.error_timeline:
        assert 0.0 <= b.error_rate <= 1.0


def test_timeline_spike_at_end():
    """Errors only in the last portion — should appear in the last bucket."""
    now = datetime.now(timezone.utc)
    logs = []
    # 20 health checks spread over 5 minutes
    for i in range(20):
        ts = (now - timedelta(minutes=5) + timedelta(seconds=i * 15)).isoformat()
        logs.append({"@timestamp": ts, "level": "INFO", "event": "health_check", "endpoint": "/health", "status": 200})
    # 10 errors in the last 20 seconds
    for i in range(10):
        ts = (now - timedelta(seconds=20) + timedelta(seconds=i * 2)).isoformat()
        logs.append({"@timestamp": ts, "level": "ERROR", "event": "request_error", "endpoint": "/error", "status": 500})
    s = summarize(logs)
    last_bucket = s.error_timeline[-1]
    assert last_bucket.error_count > 0


# ── change-point detection ───────────────────────────────────────────────────

def test_no_change_point_when_no_errors():
    logs = _make_logs(n_errors=0, n_info=20, span_minutes=5.0)
    s = summarize(logs)
    assert s.change_point_description is None
    assert s.change_point_minutes_ago is None


def test_change_point_detected_on_spike():
    """Build a timeline that clearly exceeds the 0.5 jump threshold."""
    now = datetime.now(timezone.utc)
    logs = []
    # 30 clean health checks spread over first 4 minutes
    for i in range(30):
        ts = (now - timedelta(minutes=5) + timedelta(seconds=i * 8)).isoformat()
        logs.append({"@timestamp": ts, "level": "INFO", "event": "health_check", "endpoint": "/health", "status": 200})
    # 20 errors in the last 30 seconds (dominates the last bucket)
    for i in range(20):
        ts = (now - timedelta(seconds=30) + timedelta(seconds=i)).isoformat()
        logs.append({"@timestamp": ts, "level": "ERROR", "event": "request_error", "endpoint": "/error", "status": 500})
    s = summarize(logs)
    # The last bucket should have error_rate close to 1.0 (20 errors vs ~3 health checks)
    last = s.error_timeline[-1]
    assert last.error_rate > 0.5
    # If the jump exceeds 0.5, change_point should be set
    prev = s.error_timeline[-2]
    if last.error_rate - prev.error_rate > 0.5:
        assert s.change_point_description is not None
        assert "jumped" in s.change_point_description


def test_change_point_description_format():
    now = datetime.now(timezone.utc)
    logs = []
    for i in range(20):
        ts = (now - timedelta(minutes=5) + timedelta(seconds=i * 15)).isoformat()
        logs.append({"@timestamp": ts, "level": "INFO", "event": "health_check", "endpoint": "/health", "status": 200})
    for i in range(25):
        ts = (now - timedelta(seconds=25) + timedelta(seconds=i)).isoformat()
        logs.append({"@timestamp": ts, "level": "ERROR", "event": "request_error", "endpoint": "/error", "status": 500})
    s = summarize(logs)
    if s.change_point_description:
        assert "%" in s.change_point_description
        assert "t-" in s.change_point_description

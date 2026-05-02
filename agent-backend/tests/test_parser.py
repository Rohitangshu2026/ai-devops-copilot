import json

from app.log_processor.parser import detect_error_type, extract_key_events


# ── detect_error_type ────────────────────────────────────────────────────────

def test_detect_dependency_error():
    logs = [{"message": "connection refused to db:5432"}]
    assert detect_error_type(logs) == "dependency_error"


def test_detect_dependency_error_econnrefused():
    logs = [{"message": "ECONNREFUSED 127.0.0.1:6379"}]
    assert detect_error_type(logs) == "dependency_error"


def test_detect_build_failure():
    logs = [{"message": "ModuleNotFoundError: no module named 'requests'"}]
    assert detect_error_type(logs) == "build_failure"


def test_detect_test_failure():
    logs = [{"message": "FAILED test_login.py::test_auth - AssertionError"}]
    assert detect_error_type(logs) == "test_failure"


def test_detect_runtime_crash():
    logs = [{"message": "Traceback (most recent call last):\n  File app.py line 12"}]
    assert detect_error_type(logs) == "runtime_crash"


def test_detect_unknown_for_generic_500():
    logs = [{"level": "ERROR", "event": "request_error", "endpoint": "/error", "status": 500}]
    assert detect_error_type(logs) == "unknown"


def test_detect_from_inner_json():
    inner = json.dumps({"event": "error", "message": "connection refused to redis"})
    logs = [{"message": inner}]
    assert detect_error_type(logs) == "dependency_error"


def test_detect_empty_logs():
    assert detect_error_type([]) == "unknown"


def test_detect_priority_order():
    # dependency_error is listed first, should win over runtime_crash
    logs = [{"message": "Traceback: connection refused to postgres"}]
    assert detect_error_type(logs) == "dependency_error"


# ── extract_key_events ───────────────────────────────────────────────────────

def test_extract_formats_event_endpoint_status():
    logs = [{"event": "request_error", "endpoint": "/api", "status": 500}]
    events = extract_key_events(logs)
    assert events == ["request_error /api → 500"]


def test_extract_formats_event_endpoint_only():
    logs = [{"event": "health_check", "endpoint": "/health"}]
    events = extract_key_events(logs)
    assert events == ["health_check /health"]


def test_extract_falls_back_to_message():
    logs = [{"message": "something unexpected happened"}]
    events = extract_key_events(logs)
    assert events == ["something unexpected happened"]


def test_extract_deduplicates():
    log = {"event": "request_error", "endpoint": "/error", "status": 500}
    events = extract_key_events([log, log, log])
    assert len(events) == 1


def test_extract_caps_at_10():
    logs = [{"event": f"evt{i}", "endpoint": f"/ep{i}", "status": 200} for i in range(20)]
    assert len(extract_key_events(logs)) == 10


def test_extract_from_inner_json():
    inner = json.dumps({"event": "request_error", "endpoint": "/crash", "status": 500, "level": "ERROR"})
    logs = [{"message": inner}]
    events = extract_key_events(logs)
    assert "request_error /crash → 500" in events


def test_extract_empty_logs():
    assert extract_key_events([]) == []

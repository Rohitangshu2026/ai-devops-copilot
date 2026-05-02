from app.log_processor.classifier import classify_severity


def _logs(*messages):
    return [{"message": m} for m in messages]


def test_critical_on_oom():
    assert classify_severity(_logs("OOMKilled: container exceeded memory"), "runtime_crash") == "critical"


def test_critical_on_panic():
    assert classify_severity(_logs("panic: runtime error index out of range"), "runtime_crash") == "critical"


def test_critical_on_data_loss():
    assert classify_severity(_logs("data loss detected in partition"), "unknown") == "critical"


def test_high_on_runtime_crash_error_type():
    # error_type alone drives high when no keyword matches
    assert classify_severity(_logs("some failure"), "runtime_crash") == "high"


def test_high_on_build_failure_error_type():
    assert classify_severity(_logs("compilation failed"), "build_failure") == "high"


def test_high_on_exception_keyword():
    assert classify_severity(_logs("Unhandled exception in worker thread"), "unknown") == "high"


def test_high_on_traceback_keyword():
    assert classify_severity(_logs("Traceback (most recent call last)"), "unknown") == "high"


def test_medium_on_timeout():
    assert classify_severity(_logs("read timeout after 30s"), "unknown") == "medium"


def test_medium_on_warning():
    assert classify_severity(_logs("WARNING: disk usage above 80%"), "unknown") == "medium"


def test_medium_on_retry():
    assert classify_severity(_logs("retry attempt 3 of 5"), "unknown") == "medium"


def test_low_for_generic_500():
    # simulate the /error endpoint scenario — no keywords match
    logs = [{"level": "ERROR", "event": "request_error", "endpoint": "/error", "status": 500, "message": ""}]
    assert classify_severity(logs, "unknown") == "low"


def test_low_for_empty_logs():
    assert classify_severity([], "unknown") == "low"


def test_critical_beats_high():
    # Both panic (critical) and traceback (high) present — critical wins
    assert classify_severity(_logs("panic: traceback detected"), "runtime_crash") == "critical"

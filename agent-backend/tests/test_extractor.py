from app.log_processor.extractor import extract_relevant

_ERROR = {"level": "ERROR", "message": "something broke"}
_WARN  = {"level": "WARNING", "message": "slow response"}
_INFO  = {"level": "INFO",  "message": "health check ok"}


def test_keeps_errors():
    logs = [_INFO, _ERROR, _INFO]
    result = extract_relevant(logs)
    assert result == [_ERROR]


def test_keeps_warnings():
    logs = [_INFO, _WARN, _INFO]
    result = extract_relevant(logs)
    assert result == [_WARN]


def test_keeps_errors_and_warnings():
    logs = [_INFO, _ERROR, _WARN, _INFO]
    result = extract_relevant(logs)
    assert _ERROR in result
    assert _WARN in result
    assert _INFO not in result


def test_fallback_to_all_when_no_high_signal():
    logs = [_INFO, _INFO]
    result = extract_relevant(logs)
    assert result == logs


def test_empty_input():
    assert extract_relevant([]) == []


def test_caps_at_50():
    logs = [_ERROR] * 60
    result = extract_relevant(logs)
    assert len(result) == 50


def test_critical_level_kept():
    log = {"level": "CRITICAL", "message": "system down"}
    result = extract_relevant([_INFO, log])
    assert result == [log]


def test_case_insensitive_level():
    log = {"level": "error", "message": "lowercase level"}
    result = extract_relevant([_INFO, log])
    assert log in result

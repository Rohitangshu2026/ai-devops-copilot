import pytest

from app.core.evaluator import validate_response, _KNOWN_ACTIONS


# ── valid responses ───────────────────────────────────────────────────────────

def test_valid_response_passes():
    result = {
        "root_causes": [{"cause": "connection refused to elasticsearch on port 9200", "confidence": 0.9}],
        "suggestion": "restart the elasticsearch container and check connectivity",
        "proposed_action": {"type": "notify", "target": "elasticsearch", "reason": "dep error"},
    }
    valid, reason = validate_response(result)
    assert valid is True
    assert reason == ""


def test_all_known_action_types_pass(parametrize_actions):
    result = {
        "root_causes": [{"cause": "simulated error on GET /error endpoint returning 500", "confidence": 0.9}],
        "suggestion": "check the /error handler and fix the exception",
        "proposed_action": {"type": parametrize_actions, "target": "svc", "reason": "x"},
    }
    valid, _ = validate_response(result)
    assert valid is True


@pytest.fixture(params=list(_KNOWN_ACTIONS))
def parametrize_actions(request):
    return request.param


# ── root_causes failures ──────────────────────────────────────────────────────

def test_empty_root_causes_list_fails():
    result = {
        "root_causes": [],
        "suggestion": "restart the pod to clear the error state",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False
    assert "root_causes" in reason


def test_missing_root_causes_key_fails():
    result = {
        "suggestion": "restart the pod",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False
    assert "root_causes" in reason


def test_root_cause_too_short_fails():
    result = {
        "root_causes": [{"cause": "err", "confidence": 0.5}],
        "suggestion": "restart the service to recover from this error",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False
    assert "too short" in reason


def test_root_cause_exactly_at_min_length_passes():
    # 15 chars — exactly the minimum
    cause = "a" * 15
    result = {
        "root_causes": [{"cause": cause, "confidence": 0.5}],
        "suggestion": "check and restart the service",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
    }
    valid, _ = validate_response(result)
    assert valid is True


# ── suggestion failures ───────────────────────────────────────────────────────

def test_suggestion_with_no_verb_fails():
    result = {
        "root_causes": [{"cause": "elasticsearch connection refused on port 9200", "confidence": 0.9}],
        "suggestion": "the elasticsearch service is down",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False
    assert "actionable verb" in reason


def test_empty_suggestion_fails():
    result = {
        "root_causes": [{"cause": "elasticsearch connection refused on port 9200", "confidence": 0.9}],
        "suggestion": "",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False


def test_suggestion_with_verb_passes():
    for verb in ["restart", "check", "investigate", "apply", "monitor"]:
        result = {
            "root_causes": [{"cause": "elasticsearch connection refused on port 9200", "confidence": 0.9}],
            "suggestion": f"please {verb} the service immediately",
            "proposed_action": {"type": "notify", "target": "svc", "reason": "x"},
        }
        valid, _ = validate_response(result)
        assert valid is True, f"verb '{verb}' should be accepted"


# ── proposed_action failures ──────────────────────────────────────────────────

def test_unknown_action_type_fails():
    result = {
        "root_causes": [{"cause": "elasticsearch connection refused on port 9200", "confidence": 0.9}],
        "suggestion": "restart the elasticsearch container",
        "proposed_action": {"type": "delete_everything", "target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False
    assert "action type" in reason


def test_missing_action_type_fails():
    result = {
        "root_causes": [{"cause": "elasticsearch connection refused on port 9200", "confidence": 0.9}],
        "suggestion": "restart the elasticsearch container",
        "proposed_action": {"target": "svc", "reason": "x"},
    }
    valid, reason = validate_response(result)
    assert valid is False


def test_missing_proposed_action_fails():
    result = {
        "root_causes": [{"cause": "elasticsearch connection refused on port 9200", "confidence": 0.9}],
        "suggestion": "restart the elasticsearch container",
    }
    valid, reason = validate_response(result)
    assert valid is False


# ── reason string ─────────────────────────────────────────────────────────────

def test_reason_is_empty_string_when_valid():
    result = {
        "root_causes": [{"cause": "connection refused to elasticsearch on port 9200", "confidence": 0.9}],
        "suggestion": "restart elasticsearch and verify connectivity",
        "proposed_action": {"type": "no_action", "target": "svc", "reason": "x"},
    }
    _, reason = validate_response(result)
    assert reason == ""


def test_reason_is_non_empty_string_when_invalid():
    valid, reason = validate_response({})
    assert valid is False
    assert isinstance(reason, str)
    assert len(reason) > 0

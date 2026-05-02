import json
import pytest

from app.llm.client import _parse_json, _guard_llm_result


# ── _parse_json ───────────────────────────────────────────────────────────────

def test_parse_plain_json():
    text = '{"root_causes": [{"cause": "db down", "confidence": 0.9}], "suggestion": "restart db", "proposed_action": {"type": "notify", "target": "svc", "reason": "test"}}'
    result = _parse_json(text)
    assert result["root_causes"][0]["cause"] == "db down"


def test_parse_json_with_leading_fence():
    text = '```json\n{"root_causes": [{"cause": "oom", "confidence": 0.8}], "suggestion": "scale up", "proposed_action": {"type": "scale_up", "target": "svc", "reason": "x"}}\n```'
    result = _parse_json(text)
    assert result["root_causes"][0]["cause"] == "oom"


def test_parse_json_with_fence_no_language():
    text = '```\n{"root_causes": [{"cause": "timeout", "confidence": 0.7}], "suggestion": "retry", "proposed_action": {"type": "trigger_retry", "target": "svc", "reason": "x"}}\n```'
    result = _parse_json(text)
    assert result["root_causes"][0]["cause"] == "timeout"


def test_parse_json_with_thinking_trace():
    thinking = (
        "*   Step 1: The logs show GET /error → 500.\n"
        "*   Step 2: This is a simulated endpoint.\n"
    )
    payload = json.dumps({
        "root_causes": [{"cause": "simulated 500 on /error", "confidence": 0.9}],
        "suggestion": "investigate /error handler",
        "proposed_action": {"type": "notify", "target": "sample-app", "reason": "dev env"},
    })
    text = f"{thinking}\n```json\n{payload}\n```"
    result = _parse_json(text)
    assert result["root_causes"][0]["cause"] == "simulated 500 on /error"


def test_parse_json_thinking_trace_without_fence():
    thinking = "*   Analysis: no obvious cause found.\n"
    payload = '{"root_causes": [{"cause": "unknown", "confidence": 0.5}], "suggestion": "investigate", "proposed_action": {"type": "no_action", "target": "svc", "reason": "x"}}'
    text = thinking + payload
    result = _parse_json(text)
    assert result["root_causes"][0]["cause"] == "unknown"


def test_parse_json_raises_on_invalid():
    with pytest.raises((json.JSONDecodeError, ValueError)):
        _parse_json("this is not json at all, no braces")


def test_parse_json_nested_objects_not_confused():
    # Ensure inner objects like proposed_action are not mistaken for top-level
    payload = {
        "root_causes": [{"cause": "db connection refused", "confidence": 0.85}],
        "suggestion": "check db",
        "proposed_action": {"type": "restart_pod", "target": "api", "reason": "timeout"},
    }
    text = json.dumps(payload)
    result = _parse_json(text)
    assert "root_causes" in result
    assert "proposed_action" in result


# ── _guard_llm_result ─────────────────────────────────────────────────────────

def test_guard_passes_through_valid_result():
    result = {
        "root_causes": [{"cause": "connection refused", "confidence": 0.9}],
        "suggestion": "restart db",
    }
    guarded = _guard_llm_result(result, ["GET /error → 500"])
    assert guarded["root_causes"][0]["cause"] == "connection refused"


def test_guard_fills_missing_root_causes():
    result = {"suggestion": "check logs"}
    guarded = _guard_llm_result(result, ["GET /error → 500"])
    assert len(guarded["root_causes"]) == 1
    assert "GET /error → 500" in guarded["root_causes"][0]["cause"]


def test_guard_fills_empty_root_causes_list():
    result = {"root_causes": [], "suggestion": "check logs"}
    guarded = _guard_llm_result(result, ["POST /api → 503"])
    assert len(guarded["root_causes"]) == 1
    assert "POST /api → 503" in guarded["root_causes"][0]["cause"]


def test_guard_fills_blank_cause_string():
    result = {"root_causes": [{"cause": "", "confidence": 0.5}]}
    guarded = _guard_llm_result(result, ["timeout on /db"])
    assert guarded["root_causes"][0]["cause"] != ""
    assert "timeout on /db" in guarded["root_causes"][0]["cause"]


def test_guard_fills_whitespace_only_cause():
    result = {"root_causes": [{"cause": "   ", "confidence": 0.5}]}
    guarded = _guard_llm_result(result, ["memory spike"])
    assert guarded["root_causes"][0]["cause"].strip() != ""


def test_guard_uses_first_key_event():
    result = {"root_causes": [{"cause": "", "confidence": 0.5}]}
    guarded = _guard_llm_result(result, ["event_a", "event_b"])
    assert "event_a" in guarded["root_causes"][0]["cause"]


def test_guard_works_with_no_key_events():
    result = {"root_causes": [{"cause": "", "confidence": 0.5}]}
    guarded = _guard_llm_result(result, [])
    assert guarded["root_causes"][0]["cause"] != ""


def test_guard_fallback_confidence_set():
    result = {"root_causes": []}
    guarded = _guard_llm_result(result, ["error event"])
    assert "confidence" in guarded["root_causes"][0]
    assert isinstance(guarded["root_causes"][0]["confidence"], float)

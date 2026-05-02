import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.client import _parse_json, _guard_llm_result, analyze, _call_anthropic
from app.log_processor.summarizer import LogSummary

from tests.conftest import (
    LLM_DEFAULT_RESULT,
    FakeAnthropicResponse,
    FakeTextBlock,
    FakeToolUseBlock,
)


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


# ── analyze() integration — uses mock_llm fixture, no real API calls ──────────

_SUMMARY = LogSummary(
    total_events=20,
    error_count=5,
    warning_count=1,
    unique_endpoints=["/error"],
    error_ratio=0.25,
    deduplicated_events=["error /error → 500 — 5×"],
    time_span_minutes=5.0,
)


async def test_analyze_returns_root_causes(mock_llm):
    result = await analyze(
        service="sample-app",
        environment="dev",
        error_type="runtime_crash",
        severity="high",
        key_events=["error /error → 500"],
        summary=_SUMMARY,
    )
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]
    mock_llm.gemini.assert_awaited_once()


async def test_analyze_calls_anthropic_for_non_gemma_model(mock_llm, monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_model", "claude-sonnet-4-6")
    await analyze(
        service="sample-app",
        environment="dev",
        error_type="dependency_error",
        severity="high",
        key_events=["GET /api → 500"],
        summary=_SUMMARY,
    )
    mock_llm.anthropic.assert_awaited_once()
    mock_llm.gemini.assert_not_awaited()


async def test_analyze_calls_gemini_for_gemma_model(mock_llm, monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    await analyze(
        service="sample-app",
        environment="dev",
        error_type="runtime_crash",
        severity="high",
        key_events=["error /error → 500"],
        summary=_SUMMARY,
    )
    mock_llm.gemini.assert_awaited_once()
    mock_llm.anthropic.assert_not_awaited()


async def test_analyze_guard_applied_when_backend_returns_empty_causes(mock_llm):
    mock_llm.gemini.return_value = {"root_causes": [], "suggestion": "check logs"}
    result = await analyze(
        service="sample-app",
        environment="dev",
        error_type="unknown",
        severity="low",
        key_events=["health_check /health → 200"],
        summary=_SUMMARY,
    )
    # _guard_llm_result must have filled in a fallback cause
    assert len(result["root_causes"]) == 1
    assert result["root_causes"][0]["cause"] != ""


async def test_analyze_custom_return_value_propagates(mock_llm):
    custom = {
        "root_causes": [{"cause": "OOMKilled: container exceeded memory limit", "confidence": 0.95}],
        "suggestion": "increase the memory limit in the deployment spec",
        "proposed_action": {"type": "scale_up", "target": "sample-app", "reason": "oom"},
    }
    mock_llm.gemini.return_value = custom
    result = await analyze(
        service="sample-app",
        environment="dev",
        error_type="runtime_crash",
        severity="critical",
        key_events=["OOMKilled"],
        summary=_SUMMARY,
    )
    assert "OOMKilled" in result["root_causes"][0]["cause"]
    assert result["proposed_action"]["type"] == "scale_up"


# ── Agentic loop — _call_anthropic ────────────────────────────────────────────
#
# These tests patch app.llm.client.AsyncAnthropic (the module-level import) to
# simulate multi-turn tool-use sequences without hitting the real API.

def _make_anthropic_mock(responses: list):
    """Return a patched AsyncAnthropic whose messages.create cycles through responses."""
    mock_client   = MagicMock()
    mock_messages = AsyncMock(side_effect=responses)
    mock_client.messages.create = mock_messages
    return mock_client, mock_messages


async def test_anthropic_single_round_no_tool_use():
    """Direct JSON response with stop_reason=end_turn — no tools called."""
    response = FakeAnthropicResponse(
        stop_reason="end_turn",
        content=[FakeTextBlock(json.dumps(LLM_DEFAULT_RESULT))],
    )
    mock_client, mock_messages = _make_anthropic_mock([response])
    with patch("app.llm.client.AsyncAnthropic", return_value=mock_client):
        result = await _call_anthropic("test prompt", "sample-app", 30)

    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]
    assert mock_messages.call_count == 1


async def test_anthropic_one_tool_round_then_answer():
    """stop_reason=tool_use on round 1, then end_turn on round 2."""
    tool_response = FakeAnthropicResponse(
        stop_reason="tool_use",
        content=[
            FakeTextBlock("Let me search the logs."),
            FakeToolUseBlock("search_logs", {"query": "connection refused"}, "tu_1"),
        ],
    )
    final_response = FakeAnthropicResponse(
        stop_reason="end_turn",
        content=[FakeTextBlock(json.dumps(LLM_DEFAULT_RESULT))],
    )
    mock_client, mock_messages = _make_anthropic_mock([tool_response, final_response])

    with patch("app.llm.client.AsyncAnthropic", return_value=mock_client):
        with patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="found: 5 errors"):
            result = await _call_anthropic("test prompt", "sample-app", 30)

    assert mock_messages.call_count == 2
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


async def test_anthropic_tool_result_appended_to_messages():
    """Verify the tool result is added as a user turn before the second API call."""
    tool_response = FakeAnthropicResponse(
        stop_reason="tool_use",
        content=[FakeToolUseBlock("get_error_frequency", {}, "tu_2")],
    )
    final_response = FakeAnthropicResponse(
        stop_reason="end_turn",
        content=[FakeTextBlock(json.dumps(LLM_DEFAULT_RESULT))],
    )
    mock_client, mock_messages = _make_anthropic_mock([tool_response, final_response])

    with patch("app.llm.client.AsyncAnthropic", return_value=mock_client):
        with patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="freq: /error 10"):
            await _call_anthropic("test", "sample-app", 30)

    # Second call's messages list should include the tool_result user turn
    second_call_messages = mock_messages.call_args_list[1][1]["messages"]
    roles = [m["role"] for m in second_call_messages]
    assert "assistant" in roles
    assert roles.count("user") == 2   # original prompt + tool result


async def test_anthropic_respects_max_tool_rounds():
    """After _MAX_TOOL_ROUNDS tool-use responses the loop stops and returns what it has."""
    from app.llm.client import _MAX_TOOL_ROUNDS

    tool_response = FakeAnthropicResponse(
        stop_reason="tool_use",
        content=[FakeToolUseBlock("search_logs", {"query": "error"}, "tu_x")],
    )
    responses = [tool_response] * (_MAX_TOOL_ROUNDS + 5)   # more than the cap
    mock_client, mock_messages = _make_anthropic_mock(responses)

    with patch("app.llm.client.AsyncAnthropic", return_value=mock_client):
        with patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="ok"):
            result = await _call_anthropic("test", "sample-app", 30)

    assert mock_messages.call_count == _MAX_TOOL_ROUNDS
    # After max rounds with no text block the result is {}; guard fills it later
    assert isinstance(result, dict)


# ── Evaluator integration inside analyze() ───────────────────────────────────

async def test_analyze_retries_on_invalid_response(mock_llm):
    """First call returns a malformed result; second call (strict=True) returns valid."""
    bad_result = {
        "root_causes": [{"cause": "x", "confidence": 0.5}],  # too short
        "suggestion": "the service is down",                   # no verb
        "proposed_action": {"type": "notify", "target": "svc", "reason": "r"},
    }
    good_result = {
        "root_causes": [{"cause": "connection refused to elasticsearch on port 9200", "confidence": 0.9}],
        "suggestion": "restart the elasticsearch container",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "r"},
    }
    mock_llm.gemini.side_effect = [bad_result, good_result]
    result = await analyze(
        service="sample-app", environment="dev", error_type="dependency_error",
        severity="high", key_events=["GET /error → 500"], summary=_SUMMARY,
    )
    assert mock_llm.gemini.call_count == 2
    assert result["root_causes"][0]["cause"] == good_result["root_causes"][0]["cause"]


async def test_analyze_forces_no_action_after_two_invalid_responses(mock_llm):
    """Both attempts return invalid responses — proposed_action.type forced to no_action."""
    bad_result = {
        "root_causes": [{"cause": "x", "confidence": 0.5}],
        "suggestion": "the service is down",
        "proposed_action": {"type": "notify", "target": "svc", "reason": "r"},
    }
    mock_llm.gemini.side_effect = [bad_result, bad_result]
    result = await analyze(
        service="sample-app", environment="dev", error_type="unknown",
        severity="low", key_events=["health_check /health → 200"], summary=_SUMMARY,
    )
    assert result["proposed_action"]["type"] == "no_action"

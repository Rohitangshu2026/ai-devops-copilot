import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.client import (
    _parse_json, _guard_llm_result, analyze, _call_anthropic, _call_gemini,
    _call_openai, _model_chain, _is_retriable, _keys_for, _provider,
)
from app.log_processor.summarizer import LogSummary

from tests.conftest import (
    LLM_DEFAULT_RESULT,
    FakeAnthropicResponse,
    FakeTextBlock,
    FakeToolUseBlock,
    FakeGeminiResponse,
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


# ── _call_gemini agentic loop (production path) ───────────────────────────────
#
# These test the Gemini path since that's what runs in production
# (LLM_MODEL=gemma-4-31b-it).  We mock asyncio.to_thread so the sync
# chat.send_message call runs inline, and mock google.generativeai to avoid
# any real network calls.

def _make_gemini_mock(responses: list):
    """
    Return (mock_genai, mock_chat) where chat.send_message cycles through responses.
    Patches google.generativeai.GenerativeModel and configure.
    """
    mock_chat  = MagicMock()
    mock_chat.send_message.side_effect = responses
    mock_model = MagicMock()
    mock_model.start_chat.return_value = mock_chat
    mock_genai = MagicMock()
    mock_genai.GenerativeModel.return_value = mock_model
    return mock_genai, mock_chat


async def _run_call_gemini(mock_genai, user_content="prompt", service="sample-app", lm=30):
    """Helper: patch genai + asyncio.to_thread and run _call_gemini."""
    async def fake_to_thread(fn, *args):
        return fn(*args)

    with patch.dict("sys.modules", {"google.generativeai": mock_genai}):
        with patch.object(asyncio, "to_thread", side_effect=fake_to_thread):
            # Also patch get_gemini_tools so it doesn't try to build real protos
            with patch("app.llm.client.get_gemini_tools", return_value=MagicMock()):
                return await _call_gemini(user_content, service, lm)


async def test_gemini_single_round_no_tool_use():
    """Response has no function_call parts — loop exits immediately."""
    response = FakeGeminiResponse(text=json.dumps(LLM_DEFAULT_RESULT))
    mock_genai, mock_chat = _make_gemini_mock([response])

    result = await _run_call_gemini(mock_genai)

    assert mock_chat.send_message.call_count == 1
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


async def test_gemini_one_tool_round_then_answer():
    """First response triggers a tool call; second response is the final answer."""
    tool_response  = FakeGeminiResponse(function_calls=[("search_logs", {"query": "error"})])
    final_response = FakeGeminiResponse(text=json.dumps(LLM_DEFAULT_RESULT))
    mock_genai, mock_chat = _make_gemini_mock([tool_response, final_response])

    with patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="5 errors found"):
        result = await _run_call_gemini(mock_genai)

    assert mock_chat.send_message.call_count == 2
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


async def test_gemini_tool_execute_called_with_correct_args():
    """execute_tool receives the function call name and args from the Gemini response."""
    tool_response  = FakeGeminiResponse(function_calls=[("get_error_frequency", {"lookback_minutes": 10})])
    final_response = FakeGeminiResponse(text=json.dumps(LLM_DEFAULT_RESULT))
    mock_genai, _ = _make_gemini_mock([tool_response, final_response])

    with patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="freq result") as mock_exec:
        await _run_call_gemini(mock_genai, service="sample-app", lm=30)

    mock_exec.assert_awaited_once_with("get_error_frequency", {"lookback_minutes": 10}, "sample-app", 30)


async def test_gemini_respects_max_tool_rounds():
    """Loop stops after _MAX_TOOL_ROUNDS even if every response has function calls."""
    from app.llm.client import _MAX_TOOL_ROUNDS

    tool_response = FakeGeminiResponse(function_calls=[("search_logs", {"query": "x"})])
    responses     = [tool_response] * (_MAX_TOOL_ROUNDS + 5)
    mock_genai, mock_chat = _make_gemini_mock(responses)

    with patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="ok"):
        await _run_call_gemini(mock_genai)

    # 1 initial call + _MAX_TOOL_ROUNDS follow-up calls (one per tool round)
    assert mock_chat.send_message.call_count == _MAX_TOOL_ROUNDS + 1


# ── Model chain and fallback ──────────────────────────────────────────────────

def test_model_chain_primary_only(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr("app.llm.client.settings.llm_model_fallback", "")
    assert _model_chain() == ["gemma-4-31b-it"]


def test_model_chain_with_fallbacks(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr("app.llm.client.settings.llm_model_fallback", "claude-haiku-4-5-20251001, gemini-2.0-flash")
    chain = _model_chain()
    assert chain[0] == "gemma-4-31b-it"
    assert "claude-haiku-4-5-20251001" in chain
    assert "gemini-2.0-flash" in chain


def test_model_chain_deduplicates_primary(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr("app.llm.client.settings.llm_model_fallback", "gemma-4-31b-it,claude-haiku-4-5-20251001")
    chain = _model_chain()
    assert chain.count("gemma-4-31b-it") == 1


# ── _provider ─────────────────────────────────────────────────────────────────

def test_provider_gemma():
    assert _provider("gemma-4-31b-it") == "google"


def test_provider_gemini():
    assert _provider("gemini-2.0-flash") == "google"


def test_provider_claude():
    assert _provider("claude-haiku-4-5-20251001") == "anthropic"


def test_provider_gpt():
    assert _provider("gpt-4o") == "openai"


def test_provider_o1():
    assert _provider("o1-preview") == "openai"


def test_provider_o3():
    assert _provider("o3-mini") == "openai"


def test_provider_o4():
    assert _provider("o4-mini") == "openai"


# ── _keys_for ─────────────────────────────────────────────────────────────────

def test_keys_for_gemma_returns_google_keys(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "goog-1,goog-2")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",     "generic")
    assert _keys_for("gemma-4-31b-it") == ["goog-1", "goog-2"]


def test_keys_for_gemini_returns_google_keys(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "goog-1")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",     "generic")
    assert _keys_for("gemini-2.0-flash") == ["goog-1"]


def test_keys_for_claude_returns_anthropic_keys(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.anthropic_api_keys", "ant-1,ant-2")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",        "generic")
    assert _keys_for("claude-haiku-4-5-20251001") == ["ant-1", "ant-2"]


def test_keys_for_openai_returns_openai_keys(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.openai_api_keys", "oai-1,oai-2")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",     "generic")
    assert _keys_for("gpt-4o") == ["oai-1", "oai-2"]


def test_keys_for_google_falls_back_to_generic(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",     "generic")
    assert _keys_for("gemma-4-31b-it") == ["generic"]


def test_keys_for_anthropic_falls_back_to_generic(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.anthropic_api_keys", "")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",        "generic")
    assert _keys_for("claude-sonnet-4-6") == ["generic"]


def test_keys_for_openai_falls_back_to_generic(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.openai_api_keys", "")
    monkeypatch.setattr("app.llm.client.settings.llm_api_key",     "generic")
    assert _keys_for("gpt-4o-mini") == ["generic"]


def test_keys_for_strips_whitespace(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", " goog-1 , goog-2 ")
    assert _keys_for("gemma-4-31b-it") == ["goog-1", "goog-2"]


def test_is_retriable_rate_limit_string():
    assert _is_retriable(Exception("429 rate limit exceeded")) is True


def test_is_retriable_quota_string():
    assert _is_retriable(Exception("quota exhausted for project")) is True


def test_is_retriable_resource_exhausted_string():
    assert _is_retriable(Exception("Resource exhausted")) is True


def test_is_retriable_regular_error():
    assert _is_retriable(ValueError("connection refused")) is False


def test_is_retriable_timeout():
    assert _is_retriable(asyncio.TimeoutError()) is False


async def test_analyze_falls_back_on_rate_limit(monkeypatch, mock_llm):
    """Primary model (all keys) raises rate-limit → fallback model is called."""
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr(
        "app.llm.client.settings.llm_model_fallback", "claude-haiku-4-5-20251001"
    )
    # Use a single key so gemini is called exactly once before model fallback
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "single-key")
    mock_llm.gemini.side_effect   = Exception("429 rate limit exceeded")
    mock_llm.anthropic.return_value = dict(LLM_DEFAULT_RESULT)

    result = await analyze(
        service="sample-app", environment="dev", error_type="dependency_error",
        severity="high", key_events=["GET /error → 500"], summary=_SUMMARY,
    )

    mock_llm.gemini.assert_awaited_once()
    mock_llm.anthropic.assert_awaited_once()
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


async def test_analyze_does_not_fallback_on_non_retriable_error(monkeypatch, mock_llm):
    """Non-rate-limit error from primary → exception propagates, fallback NOT tried."""
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr(
        "app.llm.client.settings.llm_model_fallback", "claude-haiku-4-5-20251001"
    )
    mock_llm.gemini.side_effect = RuntimeError("elasticsearch is down")

    with pytest.raises(RuntimeError, match="elasticsearch is down"):
        await analyze(
            service="sample-app", environment="dev", error_type="dependency_error",
            severity="high", key_events=["GET /error → 500"], summary=_SUMMARY,
        )

    mock_llm.anthropic.assert_not_awaited()


# ── Key rotation tests ────────────────────────────────────────────────────────

async def test_key_rotation_second_key_succeeds(monkeypatch, mock_llm):
    """First key raises rate-limit; second key of the same model succeeds."""
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr("app.llm.client.settings.llm_model_fallback", "")
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "key-1,key-2")

    # First call raises rate-limit, second returns valid result
    mock_llm.gemini.side_effect = [
        Exception("429 rate limit exceeded"),
        dict(LLM_DEFAULT_RESULT),
    ]

    result = await analyze(
        service="sample-app", environment="dev", error_type="runtime_crash",
        severity="high", key_events=["OOM"], summary=_SUMMARY,
    )

    assert mock_llm.gemini.await_count == 2
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


async def test_key_rotation_all_keys_exhausted_falls_back_to_next_model(monkeypatch, mock_llm):
    """All keys for primary model exhausted → fallback model is tried."""
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr(
        "app.llm.client.settings.llm_model_fallback", "claude-haiku-4-5-20251001"
    )
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "key-1,key-2")

    # Both gemini calls fail with rate-limit
    mock_llm.gemini.side_effect = Exception("429 rate limit exceeded")
    mock_llm.anthropic.return_value = dict(LLM_DEFAULT_RESULT)

    result = await analyze(
        service="sample-app", environment="dev", error_type="runtime_crash",
        severity="high", key_events=["OOM"], summary=_SUMMARY,
    )

    assert mock_llm.gemini.await_count == 2   # both keys tried
    mock_llm.anthropic.assert_awaited_once()
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


async def test_key_rotation_non_retriable_does_not_rotate(monkeypatch, mock_llm):
    """Non-retriable error skips key rotation and propagates immediately."""
    monkeypatch.setattr("app.llm.client.settings.llm_model", "gemma-4-31b-it")
    monkeypatch.setattr("app.llm.client.settings.google_api_keys", "key-1,key-2")

    mock_llm.gemini.side_effect = RuntimeError("internal server error")

    with pytest.raises(RuntimeError, match="internal server error"):
        await analyze(
            service="sample-app", environment="dev", error_type="runtime_crash",
            severity="high", key_events=["OOM"], summary=_SUMMARY,
        )

    assert mock_llm.gemini.await_count == 1   # stopped after first key


# ── _call_openai ──────────────────────────────────────────────────────────────

def _make_openai_response(content: str | None, tool_calls=None, finish_reason: str = "stop"):
    """Build a minimal fake openai ChatCompletion response."""
    import types
    tc_obj = None
    if tool_calls:
        tc_list = []
        for tc_id, name, args in tool_calls:
            fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
            tc_list.append(types.SimpleNamespace(id=tc_id, type="function", function=fn))
        tc_obj = tc_list

    message = MagicMock()
    message.content = content
    message.tool_calls = tc_obj

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "tool_calls" if tc_obj else finish_reason

    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.mark.asyncio
async def test_call_openai_no_tool_use():
    """Direct JSON answer — no tool calls."""
    payload = json.dumps(LLM_DEFAULT_RESULT)
    fake_response = _make_openai_response(payload)

    mock_create = AsyncMock(return_value=fake_response)
    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await _call_openai("analyze this", "svc", 30, "gpt-4o-mini", "key-x")

    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]
    mock_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_openai_single_tool_round():
    """One tool call then a final answer."""
    tool_resp = _make_openai_response(
        None,
        tool_calls=[("tc_1", "search_logs", {"query": "connection refused"})],
    )
    final_resp = _make_openai_response(json.dumps(LLM_DEFAULT_RESULT))

    mock_create = AsyncMock(side_effect=[tool_resp, final_resp])
    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="3 errors") as mock_tool,
    ):
        result = await _call_openai("analyze this", "svc", 30, "gpt-4o-mini", "key-x")

    assert mock_create.await_count == 2
    mock_tool.assert_awaited_once_with("search_logs", {"query": "connection refused"}, "svc", 30)
    assert result["root_causes"][0]["cause"] == LLM_DEFAULT_RESULT["root_causes"][0]["cause"]


@pytest.mark.asyncio
async def test_call_openai_respects_max_tool_rounds():
    """Capped at _MAX_TOOL_ROUNDS even if model keeps requesting tools."""
    from app.llm.client import _MAX_TOOL_ROUNDS

    tool_resp = _make_openai_response(
        None,
        tool_calls=[("tc_1", "get_error_frequency", {})],
    )
    # All responses are tool-use so the loop hits the cap
    responses = [tool_resp] * (_MAX_TOOL_ROUNDS + 1)

    mock_create = AsyncMock(side_effect=responses)
    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("app.llm.client.execute_tool", new_callable=AsyncMock, return_value="freq data"),
    ):
        result = await _call_openai("analyze this", "svc", 30, "gpt-4o-mini", "key-x")

    assert mock_create.await_count == _MAX_TOOL_ROUNDS
    # result only contains _tool_calls; no LLM text content was produced
    result_without_meta = {k: v for k, v in result.items() if k != "_tool_calls"}
    assert result_without_meta == {}
    assert "_tool_calls" in result


@pytest.mark.asyncio
async def test_call_openai_uses_openai_tools_schema():
    """Verify OPENAI_TOOLS (function-calling envelope) is passed, not TOOLS."""
    from app.llm.tools import OPENAI_TOOLS

    payload = json.dumps(LLM_DEFAULT_RESULT)
    fake_response = _make_openai_response(payload)

    mock_create = AsyncMock(return_value=fake_response)
    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await _call_openai("analyze this", "svc", 30, "gpt-4o-mini", "key-x")

    _, kwargs = mock_create.call_args
    assert kwargs["tools"] == OPENAI_TOOLS
    assert kwargs["tools"][0]["type"] == "function"
    assert "name" in kwargs["tools"][0]["function"]

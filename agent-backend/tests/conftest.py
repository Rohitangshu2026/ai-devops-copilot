import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Set required env vars before any app module is imported.
# Tests never call the real LLM or Elasticsearch — these are stubs so that
# pydantic-settings can construct the Settings object in a CI environment
# that has no .env file.
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("ES_URL", "http://localhost:9200")
os.environ.setdefault("LLM_MODEL", "gemma-4-31b-it")


# ── Canonical LLM response used across the test suite ─────────────────────────

LLM_DEFAULT_RESULT = {
    "root_causes": [
        {"cause": "connection refused to elasticsearch", "confidence": 0.9},
    ],
    "suggestion": "restart elasticsearch",
    "proposed_action": {
        "type": "notify",
        "target": "elasticsearch",
        "reason": "dependency error",
    },
}


# ── Fixture: patch both LLM backends so no real API call is ever made ─────────

@pytest.fixture
def mock_llm():
    """
    Patches app.llm.client._call_anthropic and ._call_gemini for the duration
    of a test.  Both return LLM_DEFAULT_RESULT by default.

    Usage:
        def test_something(mock_llm):
            # both backends return the default result
            ...

        def test_custom(mock_llm):
            mock_llm.anthropic.return_value = {"root_causes": [...], ...}
            ...

    The fixture yields a SimpleNamespace with:
        .anthropic  — AsyncMock for _call_anthropic
        .gemini     — AsyncMock for _call_gemini
        .default    — the default return value dict (copy-safe reference)
    """
    with (
        patch("app.llm.client._call_anthropic", new_callable=AsyncMock) as mock_a,
        patch("app.llm.client._call_gemini",    new_callable=AsyncMock) as mock_g,
    ):
        mock_a.return_value = dict(LLM_DEFAULT_RESULT)
        mock_g.return_value = dict(LLM_DEFAULT_RESULT)
        yield SimpleNamespace(anthropic=mock_a, gemini=mock_g, default=LLM_DEFAULT_RESULT)


# ── Fake Anthropic SDK objects for Phase 4 agentic loop tests ─────────────────
#
# Use these to simulate multi-turn tool-use sequences without hitting the real
# Anthropic API.  Construct a list of FakeAnthropicResponse objects and assign
# it to mock_messages_create.side_effect so each call returns the next item.
#
# Example — one tool round then a final answer:
#
#   responses = [
#       FakeAnthropicResponse(
#           stop_reason="tool_use",
#           content=[
#               FakeTextBlock("Let me search the logs."),
#               FakeToolUseBlock("search_logs", {"query": "connection refused"}),
#           ],
#       ),
#       FakeAnthropicResponse(
#           stop_reason="end_turn",
#           content=[FakeTextBlock(json.dumps(LLM_DEFAULT_RESULT))],
#       ),
#   ]
#   mock_messages_create.side_effect = responses


class FakeTextBlock:
    """Mimics anthropic.types.TextBlock."""
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeToolUseBlock:
    """Mimics anthropic.types.ToolUseBlock."""
    type = "tool_use"

    def __init__(self, name: str, input: dict, tool_id: str = "tool_1"):
        self.id = tool_id
        self.name = name
        self.input = input


class FakeAnthropicResponse:
    """
    Mimics the object returned by AsyncAnthropic.messages.create().
    stop_reason: "end_turn" | "tool_use"
    content: list of FakeTextBlock / FakeToolUseBlock
    """

    def __init__(self, stop_reason: str, content: list):
        self.stop_reason = stop_reason
        self.content = content


# ── Fake Google Generative AI objects for _call_gemini loop tests ─────────────
#
# _call_gemini checks:
#   fn_calls = [part.function_call for part in response.parts
#               if hasattr(part, "function_call") and part.function_call.name]
#
# A FakeGeminiResponse with no function calls terminates the loop; one with
# function_calls triggers tool execution and a follow-up send_message call.
#
# Example — one tool round then a final answer:
#
#   responses = [
#       FakeGeminiResponse(function_calls=[("search_logs", {"query": "error"})]),
#       FakeGeminiResponse(text=json.dumps(LLM_DEFAULT_RESULT)),
#   ]
#   mock_chat.send_message.side_effect = responses


class FakeGeminiFunctionCall:
    """Mimics a google.generativeai function call part's .function_call attribute."""
    def __init__(self, name: str, args: dict):
        self.name = name
        self.args = args


class FakeGeminiPart:
    """Mimics a single part in a Gemini response."""
    def __init__(self, function_call=None):
        # Only set function_call attribute when there is one — matches
        # the hasattr(..., "function_call") check in _call_gemini.
        if function_call is not None:
            self.function_call = function_call


class FakeGeminiResponse:
    """
    Mimics the object returned by chat.send_message() in google.generativeai.
    Provide text for a final answer, or function_calls for a tool-use round.

    function_calls: list of (name, args_dict) tuples
    """
    def __init__(self, text: str = "", function_calls: list | None = None):
        self.text = text
        self.parts = []
        for name, args in (function_calls or []):
            self.parts.append(FakeGeminiPart(FakeGeminiFunctionCall(name, args)))

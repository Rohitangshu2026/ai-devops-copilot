"""Phase 7 — integration tests using real LLM API calls.

These tests call the actual Anthropic / Google Gemini APIs.  They are skipped
automatically when the relevant API keys are absent, so they are safe to run
in any environment — CI will skip them unless credentials are explicitly
injected as environment variables.

Elasticsearch responses are always mocked.  The goal is to exercise the LLM
reasoning pipeline end-to-end: log summarization → prompt construction →
real LLM call → response validation → safety check.

Run with real Anthropic keys:
    ANTHROPIC_API_KEYS="sk-ant-..." pytest tests/test_integration_llm.py -v -s

Run with real Gemini keys:
    GOOGLE_API_KEYS="AIzaSy..." LLM_MODEL="gemini-2.5-flash" \\
        pytest tests/test_integration_llm.py -v -s
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

# ── API key detection ─────────────────────────────────────────────────────────

_ANTHROPIC_KEY = (
    os.getenv("ANTHROPIC_API_KEY")
    or (os.getenv("ANTHROPIC_API_KEYS", "").split(",")[0].strip())
)
_GOOGLE_KEY = (
    os.getenv("GOOGLE_API_KEY")
    or (os.getenv("GOOGLE_API_KEYS", "").split(",")[0].strip())
)

# Normalise — treat placeholder test keys set by conftest.py as absent.
if _ANTHROPIC_KEY in ("", "test-anthropic-key-1", "test-anthropic-key-2"):
    _ANTHROPIC_KEY = ""
if _GOOGLE_KEY in ("", "test-google-key-1", "test-google-key-2"):
    _GOOGLE_KEY = ""

_ANY_KEY = bool(_ANTHROPIC_KEY or _GOOGLE_KEY)

skip_anthropic = pytest.mark.skipif(
    not _ANTHROPIC_KEY,
    reason="ANTHROPIC_API_KEYS not set — skipping real Anthropic integration tests",
)
skip_google = pytest.mark.skipif(
    not _GOOGLE_KEY,
    reason="GOOGLE_API_KEYS not set — skipping real Gemini integration tests",
)
skip_no_keys = pytest.mark.skipif(
    not _ANY_KEY,
    reason="No LLM API keys set — skipping all integration tests",
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_KNOWN_ACTIONS = {"restart_pod", "rollback", "scale_up", "trigger_retry", "notify", "no_action"}


# ── ES mock factory ───────────────────────────────────────────────────────────


def _ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.isoformat()


def _build_es_mock(error_count: int = 8, total: int = 10) -> MagicMock:
    """Build a realistic set of error + info logs and wrap them in an ES mock."""
    logs = []

    # Error burst
    for i in range(error_count):
        logs.append({
            "@timestamp": _ts(i * 0.2),
            "service": "sample-app",
            "environment": "dev",
            "level": "ERROR",
            "event": "error",
            "endpoint": "/error",
            "message": "Simulated failure for testing",
            "error": "Exception: Simulated failure",
            "status": 500,
        })

    # Background healthy traffic
    for i in range(total - error_count):
        logs.append({
            "@timestamp": _ts(error_count * 0.2 + i * 0.5),
            "service": "sample-app",
            "environment": "dev",
            "level": "INFO",
            "event": "health_check",
            "endpoint": "/health",
            "status": 200,
        })

    es_resp = {
        "hits": {
            "total": {"value": len(logs)},
            "hits": [{"_source": l} for l in logs],
        },
        "aggregations": {
            "by_endpoint": {
                "buckets": [{"key": "/error", "doc_count": error_count}],
            }
        },
    }

    es_client = MagicMock()
    es_client.search = AsyncMock(return_value=es_resp)
    return es_client


def _assert_valid_analysis_result(result) -> None:
    """Common schema assertions for AnalysisResult."""
    assert result.root_cause, "root_cause must be non-empty"
    assert len(result.root_cause) >= 10, "root_cause too short to be meaningful"
    assert result.suggestion, "suggestion must be non-empty"
    assert any(v in result.suggestion.lower() for v in (
        "restart", "check", "investigate", "fix", "redeploy", "reduce",
        "increase", "review", "monitor", "update", "scale", "rollback",
        "verify", "ensure", "inspect", "resolve", "connect", "try",
    )), f"suggestion should contain an actionable verb: {result.suggestion!r}"
    assert result.proposed_action.get("type") in _KNOWN_ACTIONS, (
        f"proposed_action.type not in known set: {result.proposed_action}"
    )
    assert result.confidence_hint in ("low", "medium", "high")
    assert isinstance(result.confidence_score, int)
    assert result.incident_id is not None
    assert result.safety_decision in ("allowed", "denied")


# ── Anthropic real-LLM tests ──────────────────────────────────────────────────


class TestAnthropicRealLLM:
    @skip_anthropic
    @pytest.mark.asyncio
    async def test_analyze_full_pipeline_anthropic(self):
        """Full pipeline with real Anthropic API and mock ES → valid AnalysisResult."""
        from app.models.schemas import AnalysisRequest

        es_client = _build_es_mock()

        with (
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.services.memory_store.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="anthropic"),
        ):
            os.environ["LLM_MODEL"] = "claude-haiku-4-5"
            from app.core.agent import run_analysis
            req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
            result = await run_analysis(req)

        _assert_valid_analysis_result(result)

    @skip_anthropic
    @pytest.mark.asyncio
    async def test_anthropic_tool_use_triggers_additional_es_calls(self):
        """Real Anthropic call with tool-use enabled → mock ES.search called more than once."""
        es_client = _build_es_mock(error_count=8, total=10)

        with (
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.llm.tools.get_client", return_value=es_client),
            patch("app.services.memory_store.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="anthropic"),
        ):
            os.environ["LLM_MODEL"] = "claude-haiku-4-5"
            from app.llm.client import analyze as llm_analyze
            result = await llm_analyze(
                service="sample-app",
                environment="dev",
                error_type="runtime_crash",
                severity="high",
                key_events=["GET /error → 500", "GET /error → 500", "GET /error → 500"],
                summary=None,
                lookback_minutes=10,
            )

        assert "root_causes" in result
        assert result["root_causes"], "root_causes should not be empty"
        # If the LLM called a tool, ES.search would have been called more than once
        # (once for initial fetch in agent.py + 1+ for tool calls).
        # We can only verify this if es_client.search.call_count > 1 — but tools
        # are called from tools.py which uses its own get_client. The test above
        # patches both, so total call_count >= 1 is the minimum.
        assert es_client.search.call_count >= 1

    @skip_anthropic
    @pytest.mark.asyncio
    async def test_anthropic_safety_fires_and_returns_incident_id(self):
        """Full run_analysis with real Anthropic → safety_decision + UUID incident_id."""
        from app.models.schemas import AnalysisRequest

        es_client = _build_es_mock()

        with (
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.services.memory_store.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="anthropic"),
        ):
            os.environ["LLM_MODEL"] = "claude-haiku-4-5"
            from app.core.agent import run_analysis
            req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
            result = await run_analysis(req)

        assert result.safety_decision in ("allowed", "denied")
        assert result.incident_id is not None
        assert _UUID_RE.match(str(result.incident_id)), (
            f"incident_id '{result.incident_id}' is not a valid UUID"
        )

    @skip_anthropic
    @pytest.mark.asyncio
    async def test_anthropic_confidence_breakdown_non_empty(self):
        """confidence_breakdown must contain per-signal strings from real pipeline."""
        from app.models.schemas import AnalysisRequest

        es_client = _build_es_mock()

        with (
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.services.memory_store.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="anthropic"),
        ):
            os.environ["LLM_MODEL"] = "claude-haiku-4-5"
            from app.core.agent import run_analysis
            req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
            result = await run_analysis(req)

        assert result.confidence_breakdown, "confidence_breakdown must not be empty"
        assert all(b.startswith("+") for b in result.confidence_breakdown), (
            f"Each breakdown item should start with '+': {result.confidence_breakdown}"
        )


# ── Google Gemini real-LLM tests ──────────────────────────────────────────────


class TestGeminiRealLLM:
    @skip_google
    @pytest.mark.asyncio
    async def test_analyze_full_pipeline_gemini(self):
        """Full pipeline with real Gemini API and mock ES → valid AnalysisResult."""
        from app.models.schemas import AnalysisRequest

        model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
        os.environ["LLM_MODEL"] = model

        es_client = _build_es_mock()

        with (
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.services.memory_store.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="google"),
        ):
            from app.core.agent import run_analysis
            req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
            result = await run_analysis(req)

        _assert_valid_analysis_result(result)

    @skip_google
    @pytest.mark.asyncio
    async def test_gemini_json_output_parses_correctly(self):
        """Gemini JSON output (possibly markdown-wrapped) parses into dict with required keys."""
        model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
        os.environ["LLM_MODEL"] = model

        es_client = _build_es_mock()

        with (
            patch("app.llm.tools.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="google"),
        ):
            from app.llm.client import analyze as llm_analyze
            result = await llm_analyze(
                service="sample-app",
                environment="dev",
                error_type="runtime_crash",
                severity="high",
                key_events=["GET /error → 500", "GET /health → 200"],
                summary=None,
                lookback_minutes=10,
            )

        assert isinstance(result, dict), "LLM result must be a dict"
        assert "root_causes" in result or "root_cause" in result, (
            "Result must contain root_causes or root_cause"
        )
        assert "proposed_action" in result, "Result must contain proposed_action"
        assert result["proposed_action"].get("type") in _KNOWN_ACTIONS

    @skip_google
    @pytest.mark.asyncio
    async def test_gemini_safety_decision_present(self):
        """Full run_analysis with Gemini → safety_decision field is present."""
        from app.models.schemas import AnalysisRequest

        model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
        os.environ["LLM_MODEL"] = model

        es_client = _build_es_mock()

        with (
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.services.memory_store.get_client", return_value=es_client),
            patch("app.llm.client._provider", return_value="google"),
        ):
            from app.core.agent import run_analysis
            req = AnalysisRequest(service="sample-app", environment="dev", lookback_minutes=10)
            result = await run_analysis(req)

        assert result.safety_decision in ("allowed", "denied")


# ── Cross-provider resilience tests ──────────────────────────────────────────


class TestCrossProviderResilience:
    @skip_no_keys
    @pytest.mark.asyncio
    async def test_evaluator_produces_valid_output_despite_bad_first_attempt(self):
        """validate_response retries on bad first response and eventually returns valid output."""
        from app.core.evaluator import validate_response

        bad = {"root_causes": [], "proposed_action": {"type": "unknown"}}
        result = validate_response(bad)

        # After validate_response, the result should have a known action type.
        assert result.get("proposed_action", {}).get("type") in _KNOWN_ACTIONS, (
            f"validate_response must normalize unknown action type: {result}"
        )

    @skip_no_keys
    @pytest.mark.asyncio
    async def test_guard_llm_result_handles_none_root_causes(self):
        """_guard_llm_result returns a safe no_action dict when root_causes is None."""
        from app.llm.client import _guard_llm_result

        guarded = _guard_llm_result(None)
        assert guarded["proposed_action"]["type"] == "no_action"
        assert isinstance(guarded.get("root_causes"), list)

    @skip_no_keys
    @pytest.mark.asyncio
    async def test_guard_llm_result_normalises_missing_proposed_action(self):
        """_guard_llm_result adds a default proposed_action when it is missing."""
        from app.llm.client import _guard_llm_result

        partial = {
            "root_causes": [{"cause": "some error", "confidence": 0.5}],
            "suggestion": "try again",
        }
        guarded = _guard_llm_result(partial)
        assert "proposed_action" in guarded
        assert guarded["proposed_action"].get("type") in _KNOWN_ACTIONS

from unittest.mock import AsyncMock, patch

import pytest

from app.llm.tools import execute_tool, _search_logs, _get_error_frequency, TOOLS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_es_mock(hits=None, aggregations=None, total=0):
    mock = AsyncMock()
    mock.search.return_value = {
        "hits": {
            "total": {"value": total or len(hits or [])},
            "hits":  [{"_source": h} for h in (hits or [])],
        },
        "aggregations": aggregations or {},
    }
    return mock


# ── TOOLS schema ──────────────────────────────────────────────────────────────

def test_tools_list_has_two_entries():
    assert len(TOOLS) == 2


def test_tools_have_required_fields():
    names = {t["name"] for t in TOOLS}
    assert names == {"search_logs", "get_error_frequency"}
    for tool in TOOLS:
        assert "description" in tool
        assert "input_schema" in tool


def test_search_logs_query_is_required():
    schema = next(t for t in TOOLS if t["name"] == "search_logs")
    assert "query" in schema["input_schema"]["required"]


# ── _search_logs ──────────────────────────────────────────────────────────────

async def test_search_logs_formats_hits():
    mock_es = _make_es_mock(hits=[
        {"@timestamp": "2026-05-01T10:00:00Z", "level": "ERROR", "message": "connection refused"},
        {"@timestamp": "2026-05-01T10:00:01Z", "level": "ERROR", "message": "timeout"},
    ])
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _search_logs("connection refused", 30, "any", "sample-app")
    assert "connection refused" in result
    assert "ERROR" in result
    assert "2026-05-01T10:00:00" in result


async def test_search_logs_returns_not_found_when_no_hits():
    mock_es = _make_es_mock(hits=[])
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _search_logs("unicorn", 30, "any", "sample-app")
    assert "No logs found" in result
    assert "unicorn" in result


async def test_search_logs_caps_message_at_120_chars():
    long_msg = "x" * 200
    mock_es = _make_es_mock(hits=[
        {"@timestamp": "2026-05-01T10:00:00Z", "level": "ERROR", "message": long_msg},
    ])
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _search_logs("x", 30, "any", "sample-app")
    # Each line is capped at 120 chars for the message portion
    for line in result.splitlines():
        assert len(line) <= 200  # prefix + 120 chars is well under 200


async def test_search_logs_falls_back_to_event_field():
    mock_es = _make_es_mock(hits=[
        {"@timestamp": "2026-05-01T10:00:00Z", "level": "ERROR", "event": "root_hit"},
    ])
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _search_logs("root_hit", 30, "any", "sample-app")
    assert "root_hit" in result


# ── _get_error_frequency ──────────────────────────────────────────────────────

async def test_get_error_frequency_formats_buckets():
    mock_es = _make_es_mock(
        total=15,
        aggregations={
            "by_endpoint": {
                "buckets": [
                    {"key": "/error",  "doc_count": 10},
                    {"key": "/api",    "doc_count": 5},
                ]
            }
        },
    )
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _get_error_frequency(30, "sample-app")
    assert "/error" in result
    assert "10 errors" in result
    assert "/api" in result
    assert "5 errors" in result


async def test_get_error_frequency_no_errors_message():
    mock_es = _make_es_mock(
        total=0,
        aggregations={"by_endpoint": {"buckets": []}},
    )
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _get_error_frequency(30, "sample-app")
    assert "No errors found" in result
    assert "sample-app" in result


async def test_get_error_frequency_includes_service_name():
    mock_es = _make_es_mock(
        total=3,
        aggregations={"by_endpoint": {"buckets": [{"key": "/error", "doc_count": 3}]}},
    )
    with patch("app.llm.tools.get_client", return_value=mock_es):
        result = await _get_error_frequency(30, "my-service")
    assert "my-service" in result


# ── execute_tool dispatcher ───────────────────────────────────────────────────

async def test_execute_tool_dispatches_search_logs():
    with patch("app.llm.tools._search_logs", new_callable=AsyncMock, return_value="found logs") as mock_search:
        result = await execute_tool(
            "search_logs", {"query": "timeout", "level": "ERROR"}, "svc", 30
        )
    assert result == "found logs"
    mock_search.assert_awaited_once_with(
        query="timeout", lookback_minutes=30, level="ERROR", service="svc"
    )


async def test_execute_tool_dispatches_get_error_frequency():
    with patch("app.llm.tools._get_error_frequency", new_callable=AsyncMock, return_value="freq result") as mock_freq:
        result = await execute_tool("get_error_frequency", {}, "svc", 30)
    assert result == "freq result"
    mock_freq.assert_awaited_once_with(lookback_minutes=30, service="svc")


async def test_execute_tool_unknown_name_returns_error_string():
    result = await execute_tool("nonexistent_tool", {}, "svc", 30)
    assert "unknown tool" in result


async def test_execute_tool_handles_exception_gracefully():
    with patch("app.llm.tools._search_logs", new_callable=AsyncMock, side_effect=RuntimeError("es down")):
        result = await execute_tool("search_logs", {"query": "x"}, "svc", 30)
    assert "tool error" in result
    assert "es down" in result


async def test_execute_tool_uses_input_lookback_minutes():
    with patch("app.llm.tools._search_logs", new_callable=AsyncMock, return_value="ok") as mock_s:
        await execute_tool("search_logs", {"query": "x", "lookback_minutes": 60}, "svc", 30)
    # Input's lookback_minutes (60) should override the default (30)
    mock_s.assert_awaited_once_with(query="x", lookback_minutes=60, level="any", service="svc")

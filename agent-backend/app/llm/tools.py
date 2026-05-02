"""Tool definitions and ES-backed executors for the Phase 4 agentic loop."""
from typing import Any

from app.services.elk_service import get_client
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("llm_tools")

# ── Anthropic tool schema ─────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "search_logs",
        "description": (
            "Search recent logs for a keyword, error pattern, or endpoint name. "
            "Use this to drill into a specific error or verify a hypothesis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "keyword or error pattern to search for",
                },
                "lookback_minutes": {
                    "type": "integer",
                    "description": "how far back to search (defaults to the analysis window)",
                },
                "level": {
                    "type": "string",
                    "enum": ["ERROR", "WARNING", "INFO", "any"],
                    "description": "filter by log level; use 'any' to search all levels",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_error_frequency",
        "description": (
            "Get error counts grouped by endpoint for the analysis window. "
            "Use this to identify which endpoints are failing most."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_minutes": {
                    "type": "integer",
                    "description": "how far back to aggregate (defaults to the analysis window)",
                },
            },
        },
    },
]


# ── Gemini tool schema (built lazily to avoid slow import at startup) ─────────

_GEMINI_TOOLS_CACHE = None


def get_gemini_tools():
    global _GEMINI_TOOLS_CACHE
    if _GEMINI_TOOLS_CACHE is None:
        import google.generativeai as genai

        search = genai.protos.FunctionDeclaration(
            name="search_logs",
            description="Search recent logs for a keyword or error pattern.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "query":            genai.protos.Schema(type=genai.protos.Type.STRING),
                    "lookback_minutes": genai.protos.Schema(type=genai.protos.Type.INTEGER),
                    "level":            genai.protos.Schema(type=genai.protos.Type.STRING),
                },
                required=["query"],
            ),
        )
        freq = genai.protos.FunctionDeclaration(
            name="get_error_frequency",
            description="Get error counts grouped by endpoint.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "lookback_minutes": genai.protos.Schema(type=genai.protos.Type.INTEGER),
                },
            ),
        )
        _GEMINI_TOOLS_CACHE = genai.protos.Tool(function_declarations=[search, freq])
    return _GEMINI_TOOLS_CACHE


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def execute_tool(
    name: str,
    tool_input: dict[str, Any],
    service: str,
    lookback_minutes: int,
) -> str:
    """Dispatch a tool call and return a plain-text result string."""
    try:
        if name == "search_logs":
            return await _search_logs(
                query=tool_input.get("query", ""),
                lookback_minutes=int(tool_input.get("lookback_minutes", lookback_minutes)),
                level=str(tool_input.get("level", "any")),
                service=service,
            )
        if name == "get_error_frequency":
            return await _get_error_frequency(
                lookback_minutes=int(tool_input.get("lookback_minutes", lookback_minutes)),
                service=service,
            )
        return f"unknown tool: {name}"
    except Exception as exc:
        logger.warning({"message": "tool_execution_error", "tool": name, "error": str(exc)})
        return f"tool error: {exc}"


# ── Executors ─────────────────────────────────────────────────────────────────

async def _search_logs(query: str, lookback_minutes: int, level: str, service: str) -> str:
    client = get_client()
    must: list = [{"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}}]
    if service:
        must.append({
            "bool": {
                "should": [
                    {"term": {"service.keyword": service}},
                    {"term": {"service": service}},
                ],
                "minimum_should_match": 1,
            }
        })
    if level != "any":
        must.append({
            "bool": {
                "should": [
                    {"term": {"level.keyword": level}},
                    {"term": {"level": level}},
                ],
                "minimum_should_match": 1,
            }
        })

    body = {
        "query": {
            "bool": {
                "must": must,
                "should": [
                    {"match": {"message": query}},
                    {"match": {"event":   query}},
                    {"match": {"error":   query}},
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 10,
    }
    resp = await client.search(index=settings.es_index, body=body)
    hits = resp["hits"]["hits"]
    if not hits:
        return f"No logs found matching '{query}'"

    lines = []
    for h in hits:
        src = h["_source"]
        ts  = str(src.get("@timestamp", ""))[:19]
        lvl = src.get("level", "?")
        msg = src.get("message") or src.get("event") or str(src)
        lines.append(f"[{ts}] {lvl}: {str(msg)[:120]}")
    return "\n".join(lines)


async def _get_error_frequency(lookback_minutes: int, service: str) -> str:
    client = get_client()
    must: list = [{"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}}]
    if service:
        must.append({
            "bool": {
                "should": [
                    {"term": {"service.keyword": service}},
                    {"term": {"service": service}},
                ],
                "minimum_should_match": 1,
            }
        })

    body = {
        "query": {
            "bool": {
                "must": must,
                "should": [
                    {"term": {"level.keyword": "ERROR"}},
                    {"term": {"level":         "ERROR"}},
                    {"term": {"level.keyword": "CRITICAL"}},
                    {"term": {"level":         "CRITICAL"}},
                ],
                "minimum_should_match": 1,
            }
        },
        "aggs": {
            "by_endpoint": {"terms": {"field": "endpoint.keyword", "size": 10}},
        },
        "size": 0,
    }
    resp  = await client.search(index=settings.es_index, body=body)
    total = resp["hits"]["total"]["value"]
    buckets = resp.get("aggregations", {}).get("by_endpoint", {}).get("buckets", [])

    if not buckets:
        return (
            f"No errors found for '{service}' in the last {lookback_minutes} minutes "
            f"(total hits: {total})"
        )

    lines = [f"Error frequency for '{service}' (last {lookback_minutes}m, {total} total errors):"]
    for b in buckets:
        lines.append(f"  {b['key']}: {b['doc_count']} errors")
    return "\n".join(lines)

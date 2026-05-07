"""Tool definitions and ES-backed executors for the Phase 4 agentic loop.

Phase 7 adds a third tool — get_k8s_events — that queries the Kubernetes
events API to surface pod crashes, OOMKills, FailedScheduling, and other
infrastructure-level events that never appear in application logs.  The tool
degrades gracefully: if the kubernetes Python package is not installed, or if
no kubeconfig / in-cluster credentials are available, it returns the sentinel
string "k8s events unavailable" instead of raising an exception.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
    {
        "name": "get_k8s_events",
        "description": (
            "Retrieve recent Kubernetes events for a service (pod, deployment, or service "
            "object). Use this to identify pod crashes, OOMKills, scheduling failures, "
            "image pull errors, or CrashLoopBackOff notices that are not visible in "
            "application logs. Returns 'k8s events unavailable' if the cluster cannot be "
            "reached (docker-compose mode, no kubeconfig, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace to search (defaults to 'default')",
                },
                "lookback_minutes": {
                    "type": "integer",
                    "description": "how far back to look for events (defaults to analysis window)",
                },
            },
        },
    },
]


# ── OpenAI tool schema (wraps TOOLS in OpenAI's function-calling envelope) ────

OPENAI_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name":        t["name"],
            "description": t["description"],
            "parameters":  t["input_schema"],
        },
    }
    for t in TOOLS
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
        k8s_events = genai.protos.FunctionDeclaration(
            name="get_k8s_events",
            description=(
                "Retrieve recent Kubernetes events for a service — pod crashes, "
                "OOMKills, scheduling failures, image pull errors."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "namespace":        genai.protos.Schema(type=genai.protos.Type.STRING),
                    "lookback_minutes": genai.protos.Schema(type=genai.protos.Type.INTEGER),
                },
            ),
        )
        _GEMINI_TOOLS_CACHE = genai.protos.Tool(function_declarations=[search, freq, k8s_events])
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
        if name == "get_k8s_events":
            return await _get_k8s_events(
                namespace=str(tool_input.get("namespace", "default")),
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


async def _get_k8s_events(namespace: str, lookback_minutes: int, service: str) -> str:
    """List recent Kubernetes events filtered by service/pod name.

    Tries in-cluster credentials first (running inside a pod), then falls back
    to the local kubeconfig.  Returns a sentinel string on *any* failure so the
    LLM agentic loop continues gracefully when k8s is not available (e.g. in
    docker-compose mode, CI without a cluster, or when the kubernetes package
    is not installed).
    """
    try:
        # Soft import — keeps the package optional for environments without k8s.
        try:
            from kubernetes import client as k8s_client  # type: ignore[import]
            from kubernetes import config as k8s_config  # type: ignore[import]
        except ImportError:
            return "k8s events unavailable"

        # In-cluster config (pod ServiceAccount) → local kubeconfig fallback.
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            try:
                k8s_config.load_kube_config()
            except Exception:
                return "k8s events unavailable"

        v1 = k8s_client.CoreV1Api()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

        # field_selector narrows the API response to events for this service.
        field_selector = f"involvedObject.name={service}" if service else ""
        resp = await asyncio.to_thread(
            v1.list_namespaced_event,
            namespace,
            field_selector=field_selector or None,
        )

        lines: list[str] = []
        for event in resp.items:
            # last_timestamp is a datetime; event_time is used for newer API versions.
            ts = event.last_timestamp or event.event_time
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
            obj_name = (event.involved_object.name if event.involved_object else "unknown")
            lines.append(
                f"[{ts_str}] {event.type}/{event.reason}: {event.message} (object: {obj_name})"
            )

        if not lines:
            return (
                f"No k8s events found for '{service}' in namespace '{namespace}' "
                f"in the last {lookback_minutes}m"
            )

        return "\n".join(lines[:20])  # cap to 20 events per call

    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "k8s_events_unavailable", "error": str(exc)})
        return "k8s events unavailable"

"""Prometheus metric objects for the agent-backend (Phase 8c).

Import lazily from other modules to avoid circular import issues.
All objects are module-level singletons; the Prometheus client library
deduplicates by name so repeated imports are safe.
"""
from __future__ import annotations

from prometheus_client import (  # noqa: F401  (re-exported for main.py convenience)
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

# ── Counters ──────────────────────────────────────────────────────────────────

analysis_total = Counter(
    "agent_analysis_total",
    "Total number of analysis runs, labelled by service and outcome",
    ["service", "outcome"],
)

llm_call_total = Counter(
    "agent_llm_call_total",
    "Total LLM API calls, labelled by provider, model and result",
    ["provider", "model", "result"],
)

safety_denials_total = Counter(
    "agent_safety_denials_total",
    "Total safety check denials, labelled by reason keyword",
    ["reason"],
)

actions_executed_total = Counter(
    "agent_actions_executed_total",
    "Total actions executed by the action executor, labelled by type and status",
    ["action_type", "status"],
)

# ── Histograms ────────────────────────────────────────────────────────────────

analysis_duration = Histogram(
    "agent_analysis_duration_seconds",
    "End-to-end latency of a full analysis run in seconds",
)

llm_call_duration = Histogram(
    "agent_llm_call_duration_seconds",
    "Latency of a single LLM API call in seconds",
    ["provider"],
)

"""Prometheus metric objects for the agent-backend (Phase 8c).

Import lazily from other modules to avoid circular import issues.
All objects are module-level singletons; the Prometheus client library
deduplicates by name so repeated imports are safe.
"""
from __future__ import annotations

from prometheus_client import (  # noqa: F401  (re-exported for main.py convenience)
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
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

# ── K8s event watcher (Phase 1 of the K8s event-driven roadmap) ──────────────
# All metrics are namespace-scoped so a future multi-namespace expansion is
# trivial.  Counter cardinality kept low: `reason` has ~20 known values.

k8s_events_received_total = Counter(
    "agent_k8s_events_received_total",
    "K8s events accepted by the watcher (reason allowlist passed)",
    ["namespace", "reason", "severity"],
)

k8s_events_skipped_total = Counter(
    "agent_k8s_events_skipped_total",
    "K8s events rejected before dispatch; reason describes why",
    ["namespace", "reason"],  # cooldown | ignore_set | unknown_reason |
                              # unresolvable_service | rate_limit | queue_full
)

k8s_analyses_triggered_total = Counter(
    "agent_k8s_analyses_triggered_total",
    "Analyses dispatched into run_analysis() by the watcher",
    ["namespace", "service", "reason"],
)

k8s_cooldown_hits_total = Counter(
    "agent_k8s_cooldown_hits_total",
    "Events suppressed by the (service, reason) 5-min cooldown",
    ["service", "reason"],
)

k8s_watcher_reconnects_total = Counter(
    "agent_k8s_watcher_reconnects_total",
    "Watcher stream reconnect attempts (exponential backoff)",
    ["namespace"],
)

k8s_events_dropped_total = Counter(
    "agent_k8s_events_dropped_total",
    "Events dropped because the bounded dispatch queue was full",
    ["namespace"],
)

k8s_queue_depth = Gauge(
    "agent_k8s_queue_depth",
    "Current depth of the watcher's dispatch queue",
    ["namespace"],
)

k8s_concurrent_analyses = Gauge(
    "agent_k8s_concurrent_analyses",
    "Number of watcher-triggered analyses currently in flight",
)

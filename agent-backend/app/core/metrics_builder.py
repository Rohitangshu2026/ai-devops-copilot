"""Aggregate evaluation metrics over the last 24 hours (Phase 8b).

All queries target ``devops-incidents-*`` and return a flat dict suitable for
JSON serialisation.  This module never raises — errors are logged and a best-
effort partial result is returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.elk_service import get_client
from app.utils.logger import get_logger

logger = get_logger("metrics_builder")

_INDEX = "devops-incidents-*"
_NON_ACTIONED_TYPES = {"notify", "no_action"}


async def compute_metrics() -> dict:
    """Aggregate over last 24h from devops-incidents-*.

    Returns a dict with the following keys:
        correct_fix_rate, false_positive_rate, rollback_frequency,
        safety_override_rate, causality_reject_rate,
        mttr_p50_seconds, mttr_p95_seconds,
        action_budget_used, frozen_services,
        total_incidents_24h, total_actioned_24h
    """
    result: dict[str, Any] = {
        "correct_fix_rate": 0.0,
        "false_positive_rate": 0.0,
        "rollback_frequency": 0.0,
        "safety_override_rate": 0.0,
        "causality_reject_rate": 0.0,
        "mttr_p50_seconds": None,
        "mttr_p95_seconds": None,
        "action_budget_used": 0,
        "frozen_services": [],
        "total_incidents_24h": 0,
        "total_actioned_24h": 0,
    }

    try:
        client = get_client()

        # ── Base: all incidents in last 24h ──────────────────────────────────
        base_query: dict[str, Any] = {
            "query": {"range": {"timestamp": {"gte": "now-24h"}}},
            "size": 0,
            "aggs": {
                "total": {"value_count": {"field": "incident_id.keyword"}},
                "safety_denied": {
                    "filter": {"term": {"safety_decision.keyword": "denied"}}
                },
                "causality_rejected": {
                    "filter": {"term": {"causality_verified": False}}
                },
                "actioned": {
                    "filter": {
                        "bool": {
                            "must": [
                                {"term": {"safety_decision.keyword": "allowed"}},
                            ],
                            "must_not": [
                                {"terms": {"proposed_action.type.keyword": list(_NON_ACTIONED_TYPES)}}
                            ],
                        }
                    },
                    "aggs": {
                        "resolved": {
                            "filter": {"term": {"outcome.keyword": "resolved"}}
                        },
                        "unknown_outcome": {
                            "filter": {
                                "bool": {
                                    "should": [
                                        {"term": {"outcome.keyword": "unknown"}},
                                    ]
                                }
                            }
                        },
                        "rollback_triggered": {
                            "filter": {"term": {"execution_result.rollback_triggered": True}}
                        },
                    },
                },
                "mttr_percentiles": {
                    "percentiles": {
                        "field": "mttr_seconds",
                        "percents": [50, 95],
                    }
                },
                "frozen": {
                    "filter": {"term": {"action_state.keyword": "CRITICAL_INTERVENTION_REQUIRED"}},
                    "aggs": {
                        "services": {
                            "terms": {"field": "service.keyword", "size": 100}
                        }
                    },
                },
            },
        }

        resp = await client.search(index=_INDEX, body=base_query)
        aggs = resp.get("aggregations", {})

        total = aggs.get("total", {}).get("value", 0)
        result["total_incidents_24h"] = total

        actioned_bucket = aggs.get("actioned", {})
        actioned = actioned_bucket.get("doc_count", 0)
        result["total_actioned_24h"] = actioned

        # correct_fix_rate = resolved / total_actioned
        if actioned > 0:
            resolved = actioned_bucket.get("resolved", {}).get("doc_count", 0)
            result["correct_fix_rate"] = round(resolved / actioned, 4)

            # false_positive_rate = (unknown outcome) / total_actioned
            fp = actioned_bucket.get("unknown_outcome", {}).get("doc_count", 0)
            result["false_positive_rate"] = round(fp / actioned, 4)

            # rollback_frequency
            rb = actioned_bucket.get("rollback_triggered", {}).get("doc_count", 0)
            result["rollback_frequency"] = round(rb / actioned, 4)

        # safety_override_rate = denied / total
        if total > 0:
            denied = aggs.get("safety_denied", {}).get("doc_count", 0)
            result["safety_override_rate"] = round(denied / total, 4)

            # causality_reject_rate = causality_verified==False / total
            cr = aggs.get("causality_rejected", {}).get("doc_count", 0)
            result["causality_reject_rate"] = round(cr / total, 4)

        # MTTR percentiles
        pct_values = (
            aggs.get("mttr_percentiles", {})
                .get("values", {})
        )
        p50 = pct_values.get("50.0")
        p95 = pct_values.get("95.0")
        result["mttr_p50_seconds"] = round(p50, 2) if p50 is not None else None
        result["mttr_p95_seconds"] = round(p95, 2) if p95 is not None else None

        # frozen services
        frozen_buckets = (
            aggs.get("frozen", {})
                .get("services", {})
                .get("buckets", [])
        )
        result["frozen_services"] = [b["key"] for b in frozen_buckets]

        # ── action_budget_used: actioned in last 1h ──────────────────────────
        hour_query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"range": {"timestamp": {"gte": "now-1h"}}},
                        {"term": {"safety_decision.keyword": "allowed"}},
                    ],
                    "must_not": [
                        {"terms": {"proposed_action.type.keyword": list(_NON_ACTIONED_TYPES)}}
                    ],
                }
            },
            "size": 0,
        }
        hour_resp = await client.count(index=_INDEX, body=hour_query)
        result["action_budget_used"] = int(hour_resp.get("count", 0))

    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "compute_metrics_failed", "error": str(exc)})

    return result

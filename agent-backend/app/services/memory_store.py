"""Elasticsearch-backed incident memory store for Phase 5 safety stack.

Write index: ``devops-incidents``
Read index:  ``devops-incidents-*``
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.elk_service import get_client
from app.utils.logger import get_logger

logger = get_logger("memory_store")

_WRITE_INDEX = "devops-incidents"
_READ_INDEX = "devops-incidents-*"


async def save_incident(incident: dict) -> str:
    """Persist an incident document and return its incident_id.

    Returns an empty string on failure so callers can always continue.
    """
    try:
        client = get_client()
        incident_id = incident.get("incident_id", "")
        resp = await client.index(
            index=_WRITE_INDEX,
            id=incident_id if incident_id else None,
            document=incident,
        )
        saved_id: str = resp.get("_id", incident_id)
        logger.info({"message": "incident_saved", "incident_id": saved_id})
        return saved_id
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "save_incident_failed", "error": str(exc)})
        return incident.get("incident_id", "")


async def update_incident(incident_id: str, fields: dict) -> None:
    """Partially update an existing incident document.

    Silently swallows exceptions — a failed update must never block the pipeline.
    """
    try:
        client = get_client()
        await client.update(
            index=_WRITE_INDEX,
            id=incident_id,
            doc=fields,
        )
        logger.info({"message": "incident_updated", "incident_id": incident_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "update_incident_failed", "incident_id": incident_id, "error": str(exc)})


async def find_recent_actions(
    service: str,
    action_type: str,
    states: list[str],
    within_seconds: int = 120,
) -> list[dict]:
    """Return recent incident docs matching service, action type and action states.

    Returns an empty list on any error.
    """
    try:
        client = get_client()
        query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"service.keyword": service}},
                        {"term": {"proposed_action.type.keyword": action_type}},
                        {"terms": {"action_state.keyword": states}},
                        {"range": {"timestamp": {"gte": f"now-{within_seconds}s"}}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": 50,
        }
        resp = await client.search(index=_READ_INDEX, body=query)
        hits = resp["hits"]["hits"]
        return [h["_source"] for h in hits]
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "find_recent_actions_failed", "error": str(exc)})
        return []


async def count_unresolved_actions(
    service: str,
    error_type: str,
    window_minutes: int = 60,
) -> int:
    """Count incidents for *service* with *error_type* in the rolling window.

    Returns 0 on any error.
    """
    try:
        client = get_client()
        query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"service.keyword": service}},
                        {"term": {"error_type.keyword": error_type}},
                        {"range": {"timestamp": {"gte": f"now-{window_minutes}m"}}},
                    ]
                }
            },
            "size": 0,
        }
        resp = await client.count(index=_READ_INDEX, body=query)
        return int(resp.get("count", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "count_unresolved_actions_failed", "error": str(exc)})
        return 0


async def is_service_frozen(service: str) -> bool:
    """Return True if the service has an incident in CRITICAL_INTERVENTION_REQUIRED state.

    Returns False on any error so a failing ES never blocks actions.
    """
    try:
        client = get_client()
        query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"service.keyword": service}},
                        {"term": {"action_state.keyword": "CRITICAL_INTERVENTION_REQUIRED"}},
                    ]
                }
            },
            "size": 1,
        }
        resp = await client.search(index=_READ_INDEX, body=query)
        total = resp["hits"]["total"]["value"]
        return total > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "is_service_frozen_failed", "error": str(exc)})
        return False


async def find_similar_incidents(
    error_type: str,
    service: str,
    top_k: int = 3,
) -> list[dict]:
    """Return the most recent incidents with the same error_type and service.

    Returns an empty list on any error.
    """
    try:
        client = get_client()
        query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"error_type.keyword": error_type}},
                        {"term": {"service.keyword": service}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": top_k,
        }
        resp = await client.search(index=_READ_INDEX, body=query)
        hits = resp["hits"]["hits"]
        return [h["_source"] for h in hits]
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "find_similar_incidents_failed", "error": str(exc)})
        return []

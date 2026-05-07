"""Elasticsearch-backed persistence layer for the safety stack.

Houses incidents (the audit log), idempotency locks, persistent verification
jobs, and execution leases.  All write paths use ``refresh="wait_for"`` so a
subsequent read by another async pod sees the document.

Indices:
  ``devops-incidents-YYYY.MM.DD``        — incident audit log (Phase 6c daily roll)
  ``devops-incidents-*``                  — read alias / pattern
  ``devops-action-locks``                 — idempotency fingerprint locks (Phase 6a)
  ``devops-pending-verifications``        — persistent verification job queue (Phase 6b)
  ``devops-leases``                       — execution lease ownership (Phase 6h)
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from app.services.elk_service import get_client
from app.utils.logger import get_logger

logger = get_logger("memory_store")

# ── Index names ──────────────────────────────────────────────────────────────
_INCIDENTS_READ = "devops-incidents-*"
_INCIDENTS_WRITE_PREFIX = "devops-incidents-"   # date-suffixed, see _write_index()
_LOCKS_INDEX = "devops-action-locks"
_VERIFICATIONS_INDEX = "devops-pending-verifications"
_LEASES_INDEX = "devops-leases"

# Compatibility shims for tests that pre-date Phase 6c.
_WRITE_INDEX = _INCIDENTS_WRITE_PREFIX + "current"   # not actually used; see _write_index()
_READ_INDEX = _INCIDENTS_READ

ILM_POLICY_NAME = "devops-incidents-policy"
ILM_POLICY: dict[str, Any] = {
    "policy": {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {"rollover": {"max_size": "1gb", "max_age": "30d"}},
            },
            "delete": {"min_age": "90d", "actions": {"delete": {}}},
        }
    }
}


def _write_index() -> str:
    """Compute the daily write index name (Phase 6c)."""
    today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    return f"{_INCIDENTS_WRITE_PREFIX}{today}"


def _hostname() -> str:
    """Pod / process identity used for lease ownership (Phase 6h)."""
    return os.environ.get("HOSTNAME", "local")


# ── Incidents ────────────────────────────────────────────────────────────────


async def save_incident(incident: dict) -> str:
    """Persist an incident document and return its incident_id.

    Writes to the daily index pattern (Phase 6c) with ``refresh="wait_for"``
    so that a subsequent read by any pod sees the document immediately.

    Returns an empty string on failure so callers can always continue.
    """
    try:
        client = get_client()
        incident_id = incident.get("incident_id", "")
        resp = await client.index(
            index=_write_index(),
            id=incident_id if incident_id else None,
            document=incident,
            refresh="wait_for",
        )
        saved_id: str = resp.get("_id", incident_id)
        logger.info({"message": "incident_saved", "incident_id": saved_id, "index": _write_index()})
        return saved_id
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "save_incident_failed", "error": str(exc)})
        return incident.get("incident_id", "")


async def update_incident(incident_id: str, fields: dict) -> None:
    """Partially update an existing incident document.

    Searches across the read pattern to locate which daily index holds the
    document, then issues an update against that specific index.
    Silently swallows exceptions so a failed update never blocks the pipeline.
    """
    try:
        client = get_client()
        # Locate which daily index holds the doc.
        find = await client.search(
            index=_INCIDENTS_READ,
            body={"query": {"term": {"incident_id.keyword": incident_id}}, "size": 1},
        )
        hits = find["hits"]["hits"]
        if not hits:
            logger.info({"message": "update_incident_not_found", "incident_id": incident_id})
            return
        target_index = hits[0]["_index"]
        await client.update(
            index=target_index,
            id=hits[0]["_id"],
            doc=fields,
            refresh="wait_for",
        )
        logger.info({"message": "incident_updated", "incident_id": incident_id, "index": target_index})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "update_incident_failed", "incident_id": incident_id, "error": str(exc)})


async def get_incident(incident_id: str) -> Optional[dict]:
    """Return a single incident document by id, or None if not found."""
    try:
        client = get_client()
        resp = await client.search(
            index=_INCIDENTS_READ,
            body={"query": {"term": {"incident_id.keyword": incident_id}}, "size": 1},
        )
        hits = resp["hits"]["hits"]
        return hits[0]["_source"] if hits else None
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "get_incident_failed", "incident_id": incident_id, "error": str(exc)})
        return None


async def find_recent_actions(
    service: str,
    action_type: str,
    states: list[str],
    within_seconds: int = 120,
) -> list[dict]:
    """Return recent incident docs matching service, action type and states.

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
        resp = await client.search(index=_INCIDENTS_READ, body=query)
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
    """Count incidents for *service* with *error_type* in the rolling window."""
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
        resp = await client.count(index=_INCIDENTS_READ, body=query)
        return int(resp.get("count", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "count_unresolved_actions_failed", "error": str(exc)})
        return 0


async def is_service_frozen(service: str) -> bool:
    """Return True if the service has an incident in CRITICAL_INTERVENTION_REQUIRED state."""
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
        resp = await client.search(index=_INCIDENTS_READ, body=query)
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
    """Return the most recent incidents with the same error_type and service."""
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
        resp = await client.search(index=_INCIDENTS_READ, body=query)
        hits = resp["hits"]["hits"]
        return [h["_source"] for h in hits]
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "find_similar_incidents_failed", "error": str(exc)})
        return []


# ── Phase 6a — Atomic idempotency lock ───────────────────────────────────────


def _action_lock_id(service: str, action_type: str) -> str:
    """Deterministic doc id for an idempotency lock.

    Identical (service, action_type) within a 60-second bucket map to the
    same id, so concurrent ``op_type=create`` calls reliably conflict.
    """
    bucket = int(datetime.now(timezone.utc).timestamp() // 60)
    fingerprint = f"{service}|{action_type}|{bucket}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


async def try_acquire_action_lock(
    service: str,
    action_type: str,
    ttl_seconds: int = 120,
) -> bool:
    """Atomically reserve the right to execute (service, action_type).

    Uses ES ``op_type=create`` — the first writer succeeds, all peers receive
    409 Conflict.  Returns True for the winner, False otherwise.

    On any other ES error (e.g. cluster unavailable) we return True so a
    failing memory store does not block the pipeline (degrades gracefully
    to legacy behaviour).
    """
    try:
        client = get_client()
        lock_id = _action_lock_id(service, action_type)
        expires = (
            datetime.now(timezone.utc).timestamp() + ttl_seconds
        )
        await client.index(
            index=_LOCKS_INDEX,
            id=lock_id,
            document={
                "service": service,
                "action_type": action_type,
                "expires_at_unix": expires,
                "owner": _hostname(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            op_type="create",
            refresh="wait_for",
        )
        logger.info({
            "message": "action_lock_acquired",
            "service": service,
            "action_type": action_type,
            "lock_id": lock_id,
            "owner": _hostname(),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        # Conflict means a peer beat us.
        msg = str(exc).lower()
        if "conflict" in msg or "version_conflict" in msg or "409" in msg:
            logger.info({
                "message": "action_lock_conflict",
                "service": service,
                "action_type": action_type,
            })
            return False
        # Any other ES error → fail-open (don't block).
        logger.warning({
            "message": "action_lock_error",
            "service": service,
            "action_type": action_type,
            "error": str(exc),
        })
        return True


async def release_action_lock(service: str, action_type: str) -> None:
    """Best-effort release of an action lock (most callers rely on TTL)."""
    try:
        client = get_client()
        lock_id = _action_lock_id(service, action_type)
        await client.delete(index=_LOCKS_INDEX, id=lock_id, refresh="wait_for", ignore=[404])
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "release_action_lock_failed", "error": str(exc)})


# ── Phase 6b — Persistent verification job queue ─────────────────────────────


async def enqueue_verification(
    incident_id: str,
    service: str,
    environment: str,
    baseline_error_ratio: float,
    delay_seconds: int = 120,
) -> bool:
    """Persist a verification job so the sweeper can pick it up after restart."""
    try:
        client = get_client()
        verify_at = datetime.now(timezone.utc).timestamp() + delay_seconds
        await client.index(
            index=_VERIFICATIONS_INDEX,
            id=incident_id,
            document={
                "incident_id": incident_id,
                "service": service,
                "environment": environment,
                "baseline_error_ratio": baseline_error_ratio,
                "verify_after_unix": verify_at,
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
            },
            refresh="wait_for",
        )
        logger.info({
            "message": "verification_enqueued",
            "incident_id": incident_id,
            "verify_after_unix": verify_at,
        })
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "enqueue_verification_failed", "incident_id": incident_id, "error": str(exc)})
        return False


async def claim_due_verifications(limit: int = 20) -> List[dict]:
    """Return verification jobs whose ``verify_after_unix`` has passed.

    The sweeper acquires a lease (Phase 6h) before running each one — we
    do not delete the doc here.  ``delete_verification_job`` is called once
    verification completes successfully.
    """
    try:
        client = get_client()
        now_unix = datetime.now(timezone.utc).timestamp()
        query: dict[str, Any] = {
            "query": {"range": {"verify_after_unix": {"lte": now_unix}}},
            "sort": [{"verify_after_unix": {"order": "asc"}}],
            "size": limit,
        }
        resp = await client.search(index=_VERIFICATIONS_INDEX, body=query)
        return [h["_source"] for h in resp["hits"]["hits"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "claim_due_verifications_failed", "error": str(exc)})
        return []


async def delete_verification_job(incident_id: str) -> None:
    """Remove a completed verification job from the queue."""
    try:
        client = get_client()
        await client.delete(
            index=_VERIFICATIONS_INDEX,
            id=incident_id,
            refresh="wait_for",
            ignore=[404],
        )
        logger.info({"message": "verification_deleted", "incident_id": incident_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "delete_verification_failed", "incident_id": incident_id, "error": str(exc)})


# ── Phase 6h — Execution lease ───────────────────────────────────────────────


async def try_acquire_lease(
    lease_id: str,
    owner: Optional[str] = None,
    ttl_seconds: int = 60,
) -> bool:
    """Atomically claim ownership of *lease_id*.

    Uses ``op_type=create``; returns False on 409 Conflict.  On any other
    ES error returns True (fail-open) so a failing store does not stall
    the pipeline.
    """
    owner = owner or _hostname()
    try:
        client = get_client()
        expires = datetime.now(timezone.utc).timestamp() + ttl_seconds
        await client.index(
            index=_LEASES_INDEX,
            id=lease_id,
            document={
                "lease_id": lease_id,
                "owner": owner,
                "expires_at_unix": expires,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            op_type="create",
            refresh="wait_for",
        )
        logger.info({"message": "lease_acquired", "lease_id": lease_id, "owner": owner})
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "conflict" in msg or "version_conflict" in msg or "409" in msg:
            logger.info({"message": "lease_conflict", "lease_id": lease_id, "owner": owner})
            return False
        logger.warning({"message": "lease_error", "lease_id": lease_id, "error": str(exc)})
        return True


async def renew_lease(
    lease_id: str,
    owner: Optional[str] = None,
    ttl_seconds: int = 60,
) -> bool:
    """Extend the lease ``expires_at_unix`` if *owner* still owns it.

    Returns True on successful renewal, False if the lease was stolen or is
    missing (the caller should abort and let the new owner finish).
    """
    owner = owner or _hostname()
    try:
        client = get_client()
        new_expires = datetime.now(timezone.utc).timestamp() + ttl_seconds
        # Update via painless script that checks owner before touching the doc.
        resp = await client.update(
            index=_LEASES_INDEX,
            id=lease_id,
            body={
                "script": {
                    "source": (
                        "if (ctx._source.owner == params.owner) { "
                        "  ctx._source.expires_at_unix = params.new_expires; "
                        "} else { ctx.op = 'noop'; }"
                    ),
                    "params": {"owner": owner, "new_expires": new_expires},
                }
            },
            refresh="wait_for",
        )
        result = resp.get("result", "")
        if result == "noop":
            logger.info({"message": "lease_renewal_stolen", "lease_id": lease_id, "owner": owner})
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "404" in msg or "not_found" in msg:
            logger.info({"message": "lease_renewal_missing", "lease_id": lease_id})
            return False
        logger.warning({"message": "lease_renewal_error", "lease_id": lease_id, "error": str(exc)})
        # Fail-open: assume the lease is still ours so the work continues.
        return True


async def release_lease(lease_id: str, owner: Optional[str] = None) -> None:
    """Release a lease only if *owner* still owns it (idempotent on errors)."""
    owner = owner or _hostname()
    try:
        client = get_client()
        # Only delete if owner matches (to avoid releasing a lease that was
        # stolen by another pod after our own lease expired).
        await client.delete_by_query(
            index=_LEASES_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"_id": lease_id}},
                            {"term": {"owner": owner}},
                        ]
                    }
                }
            },
            refresh=True,
        )
        logger.info({"message": "lease_released", "lease_id": lease_id, "owner": owner})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "lease_release_failed", "lease_id": lease_id, "error": str(exc)})


async def recover_orphaned_leases(older_than_seconds: int = 90) -> List[str]:
    """Find and delete leases whose TTL expired without renewal.

    Returns the list of orphaned lease ids (for audit logging).  Called by
    the startup sweeper so verification jobs whose owner died can be
    re-claimed by the next pod.
    """
    try:
        client = get_client()
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
        # Search first so we can log the IDs being recovered.
        resp = await client.search(
            index=_LEASES_INDEX,
            body={
                "query": {"range": {"expires_at_unix": {"lt": cutoff}}},
                "size": 100,
            },
        )
        ids = [h["_id"] for h in resp["hits"]["hits"]]
        if ids:
            await client.delete_by_query(
                index=_LEASES_INDEX,
                body={"query": {"range": {"expires_at_unix": {"lt": cutoff}}}},
                refresh=True,
            )
            logger.info({"message": "orphaned_leases_recovered", "count": len(ids), "ids": ids})
        return ids
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "recover_orphans_failed", "error": str(exc)})
        return []


# ── ILM bootstrap (Phase 6c) ─────────────────────────────────────────────────


async def bootstrap_ilm_policy() -> bool:
    """Install the incident-retention ILM policy if it does not already exist.

    Idempotent — safe to call on every startup.  Returns True if the policy
    was installed (or already present), False on hard failure.
    """
    try:
        client = get_client()
        # _ilm/policy/<name> — use the low-level transport since the typed
        # client may not expose the ILM helpers.
        await client.transport.perform_request(
            "PUT",
            f"/_ilm/policy/{ILM_POLICY_NAME}",
            body=ILM_POLICY,
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        logger.info({"message": "ilm_policy_bootstrapped", "policy": ILM_POLICY_NAME})
        return True
    except Exception as exc:  # noqa: BLE001
        # Many ES distributions (e.g. OSS) lack ILM — log but never crash.
        logger.warning({"message": "ilm_policy_bootstrap_failed", "error": str(exc)})
        return False

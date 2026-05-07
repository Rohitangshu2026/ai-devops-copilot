"""Impact verification — checks whether an action actually resolved the incident.

Phase 6b: ``schedule_verification`` now persists the job to Elasticsearch via
``enqueue_verification`` rather than spawning a fire-and-forget asyncio task.
A startup sweeper in ``app/main.py`` polls the queue every ~30 s, claims due
jobs (lease-protected for multi-replica safety, Phase 6h), runs
``verify_resolution``, updates the incident, and deletes the job doc.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.core.policy import get_policy
from app.log_processor.summarizer import summarize
from app.services.elk_service import fetch_logs
from app.services.memory_store import (
    claim_due_verifications,
    delete_verification_job,
    enqueue_verification,
    release_lease,
    renew_lease,
    try_acquire_lease,
    update_incident,
)
from app.utils.logger import get_logger

logger = get_logger("impact")

# Allow tests to override the sweep interval.
_SWEEP_INTERVAL_SECONDS = 30


async def verify_resolution(
    service: str,
    environment: str,
    baseline_error_ratio: float,
    window_minutes: int = 2,
) -> str:
    """Fetch recent logs and compare the current error ratio to the baseline.

    Returns:
        ``"resolved"``   — current_ratio < 0.3
        ``"partial"``    — current_ratio < baseline * 0.5
        ``"unresolved"`` — otherwise
    """
    try:
        logs = await fetch_logs(service, environment, lookback_minutes=window_minutes)
        if not logs:
            logger.info({"message": "impact_no_logs", "service": service})
            return "unresolved"

        summary = summarize(logs)
        current_ratio = summary.error_ratio

        logger.info({
            "message": "impact_check",
            "service": service,
            "baseline_error_ratio": baseline_error_ratio,
            "current_ratio": current_ratio,
        })

        if current_ratio < 0.3:
            return "resolved"
        if current_ratio < baseline_error_ratio * 0.5:
            return "partial"
        return "unresolved"
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "verify_resolution_failed", "service": service, "error": str(exc)})
        return "unresolved"


async def schedule_verification(
    incident_id: str,
    service: str,
    environment: str,
    baseline_error_ratio: float,
    delay_seconds: int = 120,
) -> None:
    """Persist a verification job that the sweeper will pick up after *delay_seconds*.

    Phase 6b: this no longer spawns an in-memory task — the job is persisted
    in Elasticsearch and survives pod restarts.  Falls back to the legacy
    ``asyncio.create_task`` path only if the persistent enqueue fails (so
    we degrade gracefully when ES is briefly unavailable).
    """
    enqueued = await enqueue_verification(
        incident_id=incident_id,
        service=service,
        environment=environment,
        baseline_error_ratio=baseline_error_ratio,
        delay_seconds=delay_seconds,
    )
    if enqueued:
        return

    # Fallback: in-memory task if ES is not reachable right now.
    logger.warning({
        "message": "verification_persist_fallback_to_memory",
        "incident_id": incident_id,
    })

    async def _verify_and_update() -> None:
        await asyncio.sleep(delay_seconds)
        outcome = await verify_resolution(service, environment, baseline_error_ratio)
        await update_incident(incident_id, {"outcome": outcome, "action_state": "verified"})

    asyncio.create_task(_verify_and_update())


async def _process_one_verification(job: dict) -> None:
    """Run verification for a single due job under a lease (Phase 6h).

    The lease prevents two pods (e.g. HPA replicas) from double-verifying.
    """
    incident_id = job.get("incident_id", "")
    if not incident_id:
        logger.warning({"message": "verification_missing_id", "job": job})
        return

    lease_id = f"verify:{incident_id}"
    ttl = get_policy().global_.lease_ttl_seconds
    if not await try_acquire_lease(lease_id, ttl_seconds=ttl):
        logger.info({"message": "verification_lease_taken", "incident_id": incident_id})
        return

    try:
        outcome = await verify_resolution(
            service=job["service"],
            environment=job["environment"],
            baseline_error_ratio=job.get("baseline_error_ratio", 0.0),
        )
        await update_incident(
            incident_id,
            {"outcome": outcome, "action_state": "verified"},
        )
        await delete_verification_job(incident_id)
        logger.info({
            "message": "verification_complete",
            "incident_id": incident_id,
            "outcome": outcome,
        })
    finally:
        await release_lease(lease_id)


async def run_verification_sweeper(stop_event: Optional[asyncio.Event] = None) -> None:
    """Background sweeper loop — claims and processes due verification jobs.

    Started as a background task in ``app/main.py`` startup.  Stop via
    ``stop_event.set()`` or by cancelling the task.
    """
    logger.info({"message": "verification_sweeper_started", "interval": _SWEEP_INTERVAL_SECONDS})
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info({"message": "verification_sweeper_stopped"})
            return
        try:
            jobs = await claim_due_verifications(limit=20)
            for job in jobs:
                await _process_one_verification(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning({"message": "verification_sweep_error", "error": str(exc)})

        # Sleep with cancellation responsiveness.
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=_SWEEP_INTERVAL_SECONDS)
                # If we get here, stop was set.
                logger.info({"message": "verification_sweeper_stopped"})
                return
            else:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            # Normal — wake-up after sleep.
            pass

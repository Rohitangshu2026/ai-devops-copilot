"""Impact verification — checks whether an action actually resolved the incident.

``schedule_verification`` fires an asyncio task that sleeps for *delay_seconds*
then compares the current error ratio to the baseline.  The task is
fire-and-forget — callers must NOT await it.

``verify_resolution`` can also be called directly for synchronous verification.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.log_processor.summarizer import summarize
from app.services.elk_service import fetch_logs
from app.services.memory_store import update_incident
from app.utils.logger import get_logger

logger = get_logger("impact")


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
            logger.info({
                "message": "impact_no_logs",
                "service": service,
                "environment": environment,
            })
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
    """Schedule a fire-and-forget impact verification task.

    Creates an asyncio task that sleeps *delay_seconds* then calls
    ``verify_resolution`` and updates the incident outcome via
    ``update_incident``.  The caller must NOT await the returned coroutine.
    """

    async def _verify_and_update() -> None:
        await asyncio.sleep(delay_seconds)
        outcome = await verify_resolution(service, environment, baseline_error_ratio)
        logger.info({
            "message": "impact_verification_complete",
            "incident_id": incident_id,
            "service": service,
            "outcome": outcome,
        })
        await update_incident(incident_id, {"outcome": outcome, "action_state": "verified"})

    asyncio.create_task(_verify_and_update())

"""Loop detector for the Phase 5 safety stack.

Detects repeated action cycles for the same service/error_type within a rolling
time window.  A high repeat count causes the service to be frozen; a medium
count escalates the action to ``notify`` only.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.memory_store import count_unresolved_actions, is_service_frozen
from app.utils.logger import get_logger

logger = get_logger("loop_detector")

_FREEZE_THRESHOLD = 5
_ESCALATE_THRESHOLD = 3


@dataclass
class LoopCheckResult:
    """Result of a loop-detection check."""

    loop_detected: bool
    freeze: bool
    count: int   # -1 when frozen via is_service_frozen (count unknown)
    reason: str


async def check_loop(
    service: str,
    error_type: str,
    window_minutes: int = 60,
) -> LoopCheckResult:
    """Check whether repeated actions constitute a loop for *service*/*error_type*.

    Priority:
    1. ``is_service_frozen`` — CRITICAL_INTERVENTION_REQUIRED state → immediate freeze.
    2. ``count >= 5``        → freeze (LOOP_DETECTED).
    3. ``count >= 3``        → loop_detected (escalate to notify).
    4. Otherwise             → ok.
    """
    frozen = await is_service_frozen(service)
    if frozen:
        logger.warning({
            "message": "service_frozen",
            "service": service,
            "error_type": error_type,
        })
        return LoopCheckResult(
            loop_detected=True,
            freeze=True,
            count=-1,
            reason=f"service '{service}' is in CRITICAL_INTERVENTION_REQUIRED state",
        )

    count = await count_unresolved_actions(service, error_type, window_minutes=window_minutes)

    if count >= _FREEZE_THRESHOLD:
        logger.warning({
            "message": "LOOP_DETECTED",
            "service": service,
            "error_type": error_type,
            "count": count,
            "window_minutes": window_minutes,
        })
        return LoopCheckResult(
            loop_detected=True,
            freeze=True,
            count=count,
            reason=f"LOOP_DETECTED: {count} actions in {window_minutes}m window for "
                   f"service='{service}' error_type='{error_type}'",
        )

    if count >= _ESCALATE_THRESHOLD:
        logger.info({
            "message": "loop_escalate",
            "service": service,
            "error_type": error_type,
            "count": count,
        })
        return LoopCheckResult(
            loop_detected=True,
            freeze=False,
            count=count,
            reason=f"repeated actions ({count} in {window_minutes}m); escalating to notify only",
        )

    return LoopCheckResult(
        loop_detected=False,
        freeze=False,
        count=count,
        reason="no loop detected",
    )

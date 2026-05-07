"""Confidence scoring engine (Phase 3 + 6g + 8a).

Returns a tuple of ``(label, score, breakdown)`` where ``breakdown`` lists the
human-readable per-signal contributions that make up the score.  Operators
can audit the score by reading the breakdown — no opaque numbers.
"""
from __future__ import annotations

from typing import List, Tuple

from app.log_processor.summarizer import LogSummary
from app.utils.logger import get_logger

logger = get_logger("confidence")


async def score_confidence(
    summary: LogSummary,
    error_type: str,
    severity: str,
    service: str = "",
) -> Tuple[str, int, List[str]]:
    """Compute a deterministic confidence score from log signals.

    Phase 8a: if *service* is provided, query historical incidents and apply a
    +2 boost when ≥2 of the 3 most similar past incidents were resolved.

    Returns:
        (label, score, breakdown) where:
            label:     "high" | "medium" | "low"
            score:     0–9 integer
            breakdown: list of "+N <signal>" strings explaining each point

    Score → label:
        score >= 7 → "high"
        score >= 4 → "medium"
        else       → "low"
    """
    score = 0
    breakdown: List[str] = []

    if error_type != "unknown":
        score += 2
        breakdown.append(f"+2 error_type={error_type} (known type)")

    if severity in ("high", "critical"):
        score += 2
        breakdown.append(f"+2 severity={severity}")

    if summary.error_ratio > 0.10:
        score += 2
        breakdown.append(f"+2 error_ratio={summary.error_ratio:.2f} (>10%)")

    if summary.error_count >= 3:
        score += 1
        breakdown.append(f"+1 error_count={summary.error_count} (≥3 distinct errors)")

    if summary.total_events >= 10:
        score += 1
        breakdown.append(f"+1 total_events={summary.total_events} (≥10)")

    if error_type in ("runtime_crash", "build_failure"):
        score += 1
        breakdown.append(f"+1 error_type={error_type} (high-signal type)")

    # ── Phase 8a — historical match boost ───────────────────────────────────
    if service:
        try:
            from app.services.memory_store import find_similar_incidents
            similar = await find_similar_incidents(error_type, service, top_k=3)
            resolved_count = sum(1 for inc in similar if inc.get("outcome") == "resolved")
            if resolved_count >= 2:
                score += 2
                breakdown.append(
                    f"+2 historical_match ({resolved_count} of {len(similar)} similar incidents resolved)"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning({"message": "historical_boost_failed", "error": str(exc)})

    if score >= 7:
        label = "high"
    elif score >= 4:
        label = "medium"
    else:
        label = "low"

    return label, score, breakdown

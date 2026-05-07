"""Confidence scoring engine (Phase 3 + 6g).

Returns a tuple of ``(label, score, breakdown)`` where ``breakdown`` lists the
human-readable per-signal contributions that make up the score.  Operators
can audit the score by reading the breakdown — no opaque numbers.
"""
from __future__ import annotations

from typing import List, Tuple

from app.log_processor.summarizer import LogSummary


def score_confidence(
    summary: LogSummary,
    error_type: str,
    severity: str,
) -> Tuple[str, int, List[str]]:
    """Compute a deterministic confidence score from log signals.

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

    if score >= 7:
        label = "high"
    elif score >= 4:
        label = "medium"
    else:
        label = "low"

    return label, score, breakdown

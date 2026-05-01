from app.log_processor.summarizer import LogSummary


def score_confidence(
    summary: LogSummary,
    error_type: str,
    severity: str,
) -> tuple[str, int]:
    score = 0

    if error_type != "unknown":
        score += 2
    if severity in ("high", "critical"):
        score += 2
    if summary.error_ratio > 0.10:
        score += 2
    if summary.error_count >= 3:
        score += 1
    if summary.total_events >= 10:
        score += 1
    if error_type in ("runtime_crash", "build_failure"):
        score += 1

    if score >= 7:
        label = "high"
    elif score >= 4:
        label = "medium"
    else:
        label = "low"

    return label, score

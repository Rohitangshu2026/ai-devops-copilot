from typing import List

_HIGH_SIGNAL_LEVELS = {"ERROR", "CRITICAL", "WARNING"}
_MAX_LOGS = 50


def extract_relevant(logs: List[dict]) -> List[dict]:
    """Return high-signal log entries capped to _MAX_LOGS."""
    filtered = [l for l in logs if str(l.get("level", "")).upper() in _HIGH_SIGNAL_LEVELS]
    if not filtered:
        filtered = logs
    return filtered[:_MAX_LOGS]

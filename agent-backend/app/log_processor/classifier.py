from typing import List

_CRITICAL_KEYWORDS = ("panic", "oom", "killed", "segfault", "data loss", "corruption")
_HIGH_KEYWORDS = ("exception", "traceback", "build fail", "test fail", "connection refused")
_MEDIUM_KEYWORDS = ("warning", "timeout", "retry", "deprecated")


def classify_severity(logs: List[dict], error_type: str) -> str:
    combined = " ".join(str(l.get("message", "")) for l in logs).lower()
    if any(k in combined for k in _CRITICAL_KEYWORDS):
        return "critical"
    if error_type in ("runtime_crash", "build_failure") or any(k in combined for k in _HIGH_KEYWORDS):
        return "high"
    if any(k in combined for k in _MEDIUM_KEYWORDS):
        return "medium"
    return "low"

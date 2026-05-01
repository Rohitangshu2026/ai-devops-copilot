import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

_DYNAMIC_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"\buser[_-]?[a-z0-9]{4,}\b", re.I), "<ID>"),
    (re.compile(r"/tmp/[^\s\"']+"), "<PATH>"),
    (re.compile(r"\b[0-9a-f]{40}\b"), "<HASH>"),
]

_NOISE_EVENTS = {"health_check"}
_TIME_BUCKET_SECONDS = 60


@dataclass
class LogSummary:
    total_events: int
    error_count: int
    warning_count: int
    unique_endpoints: List[str]
    error_ratio: float
    deduplicated_events: List[str]
    time_span_minutes: float
    has_only_noise: bool = False


def _mask(text: str) -> str:
    for pattern, token in _DYNAMIC_PATTERNS:
        text = pattern.sub(token, text)
    return text


def _parse_inner(log: dict) -> dict:
    msg = log.get("message", "")
    if isinstance(msg, str) and msg.startswith("{"):
        try:
            inner = json.loads(msg)
            merged = {**log, **inner}
            merged.pop("message", None)
            return merged
        except (json.JSONDecodeError, ValueError):
            pass
    return log


def _to_readable(log: dict) -> str:
    event = log.get("event", "")
    endpoint = log.get("endpoint", "")
    status = log.get("status", "")
    message = log.get("message", "")
    level = log.get("level", "")

    if event and endpoint and status:
        return _mask(f"{event} {endpoint} → {status}")
    if event and endpoint:
        return _mask(f"{event} {endpoint}")
    if message:
        return _mask(str(message))
    return _mask(str(log))


def _bucket(ts_str: str) -> int:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp()) // _TIME_BUCKET_SECONDS
    except (ValueError, TypeError):
        return 0


def summarize(logs: List[dict]) -> LogSummary:
    if not logs:
        return LogSummary(0, 0, 0, [], 0.0, [], 0.0, has_only_noise=True)

    parsed = [_parse_inner(l) for l in logs]

    timestamps = []
    for l in parsed:
        ts = l.get("@timestamp") or l.get("timestamp")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            except (ValueError, TypeError):
                pass

    time_span = 0.0
    if len(timestamps) >= 2:
        time_span = (max(timestamps) - min(timestamps)).total_seconds() / 60

    error_count = sum(1 for l in parsed if str(l.get("level", "")).upper() in ("ERROR", "CRITICAL"))
    warning_count = sum(1 for l in parsed if str(l.get("level", "")).upper() == "WARNING")
    unique_endpoints = list({l.get("endpoint", "") for l in parsed if l.get("endpoint")})
    error_ratio = error_count / len(parsed) if parsed else 0.0

    non_noise = [l for l in parsed if l.get("event") not in _NOISE_EVENTS
                 or str(l.get("level", "")).upper() in ("ERROR", "CRITICAL", "WARNING")]
    signal_logs = non_noise if non_noise else parsed
    has_only_noise = not non_noise

    # deduplicate with time-bucketing
    seen: dict[tuple, dict] = {}
    for log in signal_logs:
        key_str = _to_readable(log)
        ts = log.get("@timestamp") or log.get("timestamp", "")
        bucket = _bucket(str(ts))
        group_key = (key_str, bucket)
        if group_key not in seen:
            seen[group_key] = {"readable": key_str, "count": 0, "level": log.get("level", "INFO")}
        seen[group_key]["count"] += 1

    # build final event strings with frequency counts
    deduped: List[str] = []
    for entry in seen.values():
        count = entry["count"]
        suffix = f" — {count}×" if count > 1 else ""
        deduped.append(f"{entry['readable']}{suffix}")

    # prioritise errors/warnings, cap at 20
    def priority(s: str) -> int:
        upper = s.upper()
        if "ERROR" in upper or "CRITICAL" in upper or "500" in upper:
            return 0
        if "WARNING" in upper or "WARN" in upper:
            return 1
        return 2

    deduped.sort(key=priority)
    deduped = deduped[:20]

    return LogSummary(
        total_events=len(parsed),
        error_count=error_count,
        warning_count=warning_count,
        unique_endpoints=unique_endpoints,
        error_ratio=round(error_ratio, 4),
        deduplicated_events=deduped,
        time_span_minutes=round(time_span, 2),
        has_only_noise=has_only_noise,
    )

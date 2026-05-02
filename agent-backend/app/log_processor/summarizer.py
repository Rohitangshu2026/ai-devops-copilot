import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

_DYNAMIC_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"\buser[_-]?[a-z0-9]{4,}\b", re.I), "<ID>"),
    (re.compile(r"/tmp/[^\s\"']+"), "<PATH>"),
    (re.compile(r"\b[0-9a-f]{40}\b"), "<HASH>"),
]

_NOISE_EVENTS = {"health_check"}
_TIMELINE_BUCKETS = 4


@dataclass
class TimelineBucket:
    bucket: str         # "t-10m", "t-5m", etc.
    error_count: int
    total_count: int
    error_rate: float


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
    # 3b additions — computed from raw timestamps, independent of dedup
    error_timeline: List[TimelineBucket] = field(default_factory=list)
    change_point_minutes_ago: Optional[float] = None
    change_point_description: Optional[str] = None


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

    if event and endpoint and status:
        return _mask(f"{event} {endpoint} → {status}")
    if event and endpoint:
        return _mask(f"{event} {endpoint}")
    if message:
        return _mask(str(message))
    return _mask(str(log))


def _parse_ts(log: dict) -> Optional[datetime]:
    ts = log.get("@timestamp") or log.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _adaptive_bucket_seconds(time_span_minutes: float) -> int:
    if time_span_minutes <= 2:
        return 10
    if time_span_minutes <= 10:
        return 30
    if time_span_minutes <= 30:
        return 60
    return 300


def _build_timeline(parsed: List[dict], ts_map: List[Optional[datetime]], time_span: float) -> List[TimelineBucket]:
    """Compute error timeline from raw (level, timestamp) pairs — never from deduped output."""
    valid = [(ts, p) for ts, p in zip(ts_map, parsed) if ts is not None]
    if len(valid) < 2:
        return []

    t_min = min(ts for ts, _ in valid)
    t_max = max(ts for ts, _ in valid)
    total_seconds = (t_max - t_min).total_seconds()
    if total_seconds < 1:
        return []

    bucket_seconds = total_seconds / _TIMELINE_BUCKETS
    buckets: List[TimelineBucket] = []

    for i in range(_TIMELINE_BUCKETS):
        b_start = t_min.timestamp() + i * bucket_seconds
        b_end = b_start + bucket_seconds
        in_bucket = [(ts, p) for ts, p in valid if b_start <= ts.timestamp() < b_end]
        # include last event in final bucket
        if i == _TIMELINE_BUCKETS - 1:
            in_bucket = [(ts, p) for ts, p in valid if b_start <= ts.timestamp() <= b_end]

        total = len(in_bucket)
        errors = sum(
            1 for _, p in in_bucket
            if str(p.get("level", "")).upper() in ("ERROR", "CRITICAL")
        )
        rate = round(errors / total, 3) if total else 0.0
        minutes_from_end = round((t_max.timestamp() - b_end) / 60, 1)
        label = f"t-{abs(minutes_from_end)}m" if minutes_from_end > 0 else "t-0m"
        buckets.append(TimelineBucket(
            bucket=label,
            error_count=errors,
            total_count=total,
            error_rate=rate,
        ))

    return buckets


def _detect_change_point(
    timeline: List[TimelineBucket],
    t_max: datetime,
    t_min: datetime,
) -> tuple[Optional[float], Optional[str]]:
    """Slide over timeline buckets; flag where error_rate jumps > 0.5."""
    for i in range(1, len(timeline)):
        prev = timeline[i - 1]
        curr = timeline[i]
        if curr.error_rate - prev.error_rate > 0.5:
            total_seconds = (t_max - t_min).total_seconds()
            bucket_seconds = total_seconds / _TIMELINE_BUCKETS
            # midpoint of the transition bucket
            minutes_ago = round((total_seconds - (i + 0.5) * bucket_seconds) / 60, 1)
            desc = (
                f"error rate jumped from {int(prev.error_rate * 100)}% "
                f"to {int(curr.error_rate * 100)}% at t-{minutes_ago}m"
            )
            return minutes_ago, desc
    return None, None


def summarize(logs: List[dict]) -> LogSummary:
    if not logs:
        return LogSummary(0, 0, 0, [], 0.0, [], 0.0, has_only_noise=True)

    parsed = [_parse_inner(l) for l in logs]
    ts_map = [_parse_ts(l) for l in parsed]

    valid_ts = [ts for ts in ts_map if ts is not None]
    time_span = 0.0
    t_min = t_max = None
    if len(valid_ts) >= 2:
        t_min, t_max = min(valid_ts), max(valid_ts)
        time_span = (t_max - t_min).total_seconds() / 60

    error_count = sum(1 for l in parsed if str(l.get("level", "")).upper() in ("ERROR", "CRITICAL"))
    warning_count = sum(1 for l in parsed if str(l.get("level", "")).upper() == "WARNING")
    unique_endpoints = list({l.get("endpoint", "") for l in parsed if l.get("endpoint")})
    error_ratio = error_count / len(parsed) if parsed else 0.0

    non_noise = [l for l in parsed if l.get("event") not in _NOISE_EVENTS
                 or str(l.get("level", "")).upper() in ("ERROR", "CRITICAL", "WARNING")]
    signal_logs = non_noise if non_noise else parsed
    has_only_noise = not non_noise

    # --- 3b-1 / 3b-3: timeline + change-point (from raw timestamps, not deduped) ---
    timeline: List[TimelineBucket] = []
    change_point_minutes_ago: Optional[float] = None
    change_point_description: Optional[str] = None
    if t_min and t_max and time_span > 0:
        timeline = _build_timeline(parsed, ts_map, time_span)
        change_point_minutes_ago, change_point_description = _detect_change_point(timeline, t_max, t_min)

    # --- 3b-2: adaptive dedup bucketing (independent of timeline) ---
    bucket_seconds = _adaptive_bucket_seconds(time_span)
    seen: dict[tuple, dict] = {}
    for log, ts in zip(signal_logs, [_parse_ts(l) for l in signal_logs]):
        key_str = _to_readable(log)
        bucket_id = int(ts.timestamp()) // bucket_seconds if ts else 0
        group_key = (key_str, bucket_id)
        if group_key not in seen:
            seen[group_key] = {"readable": key_str, "count": 0, "level": log.get("level", "INFO")}
        seen[group_key]["count"] += 1

    deduped: List[str] = []
    for entry in seen.values():
        count = entry["count"]
        suffix = f" — {count}×" if count > 1 else ""
        deduped.append(f"{entry['readable']}{suffix}")

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
        error_timeline=timeline,
        change_point_minutes_ago=change_point_minutes_ago,
        change_point_description=change_point_description,
    )

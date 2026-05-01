import json
import re
from typing import List

_PATTERNS = [
    ("dependency_error", re.compile(r"connection refused|timeout|ECONNREFUSED|no such host|dns", re.I)),
    ("build_failure",    re.compile(r"build fail|compilation error|syntax error|import error|modulenotfound", re.I)),
    ("test_failure",     re.compile(r"assert(ion)?error|test fail|FAILED|pytest", re.I)),
    ("runtime_crash",    re.compile(r"traceback|exception|panic|segfault|oom|killed", re.I)),
]


def _unwrap(log: dict) -> dict:
    """Parse inner JSON from the message field if present."""
    msg = log.get("message", "")
    if isinstance(msg, str) and msg.startswith("{"):
        try:
            return {**log, **json.loads(msg)}
        except (json.JSONDecodeError, ValueError):
            pass
    return log


def _readable(log: dict) -> str:
    event = log.get("event", "")
    endpoint = log.get("endpoint", "")
    status = log.get("status", "")
    message = log.get("message", "")
    if event and endpoint and status:
        return f"{event} {endpoint} → {status}"
    if event and endpoint:
        return f"{event} {endpoint}"
    if message:
        return str(message)[:120]
    return str(log)[:120]


def detect_error_type(logs: List[dict]) -> str:
    unwrapped = [_unwrap(l) for l in logs]
    combined = " ".join(
        f"{l.get('message', '')} {l.get('event', '')} {l.get('error', '')}"
        for l in unwrapped
    )
    for name, pattern in _PATTERNS:
        if pattern.search(combined):
            return name
    return "unknown"


def extract_key_events(logs: List[dict]) -> List[str]:
    events: List[str] = []
    for log in logs:
        entry = _readable(_unwrap(log))
        if entry and entry not in events:
            events.append(entry)
        if len(events) >= 10:
            break
    return events

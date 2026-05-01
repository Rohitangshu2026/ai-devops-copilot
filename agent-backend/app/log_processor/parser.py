import re
from typing import List

_PATTERNS = [
    ("dependency_error", re.compile(r"connection refused|timeout|ECONNREFUSED|no such host|dns", re.I)),
    ("build_failure",    re.compile(r"build fail|compilation error|syntax error|import error|modulenotfound", re.I)),
    ("test_failure",     re.compile(r"assert(ion)?error|test fail|FAILED|pytest", re.I)),
    ("runtime_crash",    re.compile(r"traceback|exception|panic|segfault|oom|killed", re.I)),
]


def detect_error_type(logs: List[dict]) -> str:
    combined = " ".join(str(l.get("message", "")) for l in logs)
    for name, pattern in _PATTERNS:
        if pattern.search(combined):
            return name
    return "unknown"


def extract_key_events(logs: List[dict]) -> List[str]:
    events: List[str] = []
    for log in logs:
        msg = log.get("message") or log.get("event") or ""
        if msg and str(msg) not in events:
            events.append(str(msg))
        if len(events) >= 10:
            break
    return events

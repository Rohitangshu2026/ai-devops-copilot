import re
from dataclasses import dataclass
from typing import List, Optional

from app.log_processor.summarizer import LogSummary

# Static dependency map — will be replaced by k8s service mesh discovery in Phase 7
DEPENDENCY_MAP: dict[str, List[str]] = {
    "sample-app": ["elasticsearch"],
    "agent-backend": ["elasticsearch"],
}

_DEPENDENCY_ERROR_PATTERNS = re.compile(
    r"connection refused|timeout|no such host|ECONNREFUSED|dns|unreachable", re.I
)

_HYPOTHESIS_EVIDENCE: dict[str, re.Pattern] = {
    "db":       re.compile(r"connection refused|timeout|no such host|db|database|postgres|mysql|mongo", re.I),
    "oom":      re.compile(r"OOMKilled|memory|out of memory|oom", re.I),
    "build":    re.compile(r"build fail|compilation error|syntax error|import error|modulenotfound", re.I),
    "test":     re.compile(r"FAILED|assertion|test fail|pytest", re.I),
    "simulated":re.compile(r"simulated|intentional|test endpoint", re.I),
}


@dataclass
class CausalityResult:
    verified: bool
    matched_evidence: List[str]
    target_redirected: bool = False
    action_target: Optional[str] = None   # None means use the requesting service


def validate_causality(
    summary: LogSummary,
    root_cause: str,
    service: str,
) -> CausalityResult:
    combined = " ".join(summary.deduplicated_events) + " " + root_cause
    matched: List[str] = []

    # check if any hypothesis pattern matches the evidence
    for label, pattern in _HYPOTHESIS_EVIDENCE.items():
        if pattern.search(combined):
            matched.append(label)

    # special case: no error signal at all → low-confidence but not blocked
    if summary.error_ratio < 0.05:
        matched.append("simulated_or_no_issue")

    verified = len(matched) > 0

    # dependency-aware target redirection
    target_redirected = False
    action_target = None
    if verified and _DEPENDENCY_ERROR_PATTERNS.search(combined):
        deps = DEPENDENCY_MAP.get(service, [])
        if deps:
            # redirect action to the first dependency with connection error signals
            action_target = deps[0]
            target_redirected = True

    return CausalityResult(
        verified=verified,
        matched_evidence=matched,
        target_redirected=target_redirected,
        action_target=action_target,
    )

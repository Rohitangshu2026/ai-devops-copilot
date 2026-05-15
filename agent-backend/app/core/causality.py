import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.log_processor.summarizer import LogSummary

# Static dependency map — historical fallback, still used when neither the
# platform registry nor live k8s annotations can answer the lookup.  The map
# is now lazily merged with platform-registered services at first use.
_STATIC_DEPENDENCY_MAP: Dict[str, List[str]] = {
    "sample-app": ["elasticsearch"],
    "agent-backend": ["elasticsearch"],
}


def _build_dependency_map() -> Dict[str, List[str]]:
    """Merge static defaults with every registered platform's service deps.

    Multi-platform refactor: platforms declare each service's dependencies
    in ``configs/platforms/*.yaml`` (see ``ServiceSpec.depends_on``).  Merging
    those into the legacy ``DEPENDENCY_MAP`` keeps the existing causality
    semantics intact while letting new platforms onboard without a code
    change.

    Live k8s annotations (read by ``blast_radius.py``) still take precedence
    when available — this map is only the static / config-file source.
    """
    merged: Dict[str, List[str]] = {k: list(v) for k, v in _STATIC_DEPENDENCY_MAP.items()}
    try:
        from app.platforms.service_registry import dependency_map as _platform_deps
        for svc, deps in _platform_deps().items():
            existing = set(merged.get(svc, []))
            existing.update(deps)
            merged[svc] = sorted(existing)
    except Exception:  # noqa: BLE001
        # Registry not initialised or yaml malformed — fall back to static map.
        pass
    return merged


class _LazyDepMap(dict):
    """Dict that refreshes from the platform registry on every read.

    Cheap (<1ms for ~10 platforms × ~5 services each) and keeps callers that
    expect a plain ``dict`` working without any refactor.  When the registry
    reloads on SIGHUP, this picks up the new values on the next lookup.
    """

    def _refresh(self) -> None:
        super().clear()
        super().update(_build_dependency_map())

    def __getitem__(self, key):  # type: ignore[override]
        self._refresh()
        return super().__getitem__(key)

    def get(self, key, default=None):  # type: ignore[override]
        self._refresh()
        return super().get(key, default)

    def __contains__(self, key):  # type: ignore[override]
        self._refresh()
        return super().__contains__(key)

    def items(self):  # type: ignore[override]
        self._refresh()
        return super().items()

    def keys(self):  # type: ignore[override]
        self._refresh()
        return super().keys()

    def values(self):  # type: ignore[override]
        self._refresh()
        return super().values()


# Public name — preserved for backward compatibility with callers that
# import ``DEPENDENCY_MAP`` directly (``blast_radius.py``, tests, etc.).
DEPENDENCY_MAP: Dict[str, List[str]] = _LazyDepMap()

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

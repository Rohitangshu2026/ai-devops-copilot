"""Policy engine: maps (error_type, severity, confidence) → allowed action set.

``apply_policy`` is the single public function.  It returns a ``DecisionResult``
that indicates whether the proposed action is allowed and what the final action
should be (possibly overridden to ``no_action``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from app.utils.logger import get_logger

logger = get_logger("decision")

# ---------------------------------------------------------------------------
# Policy table — checked in order; first match wins.
# Each row: ((error_type, severity, confidence), allowed_actions)
# "*" is a wildcard that matches any value.
# ---------------------------------------------------------------------------
ACTION_POLICY: List[Tuple[Tuple[str, str, str], List[str]]] = [
    (("runtime_crash",    "critical", "high"),   ["restart_pod", "rollback"]),
    (("runtime_crash",    "high",     "high"),   ["restart_pod"]),
    (("runtime_crash",    "high",     "medium"), ["notify"]),
    (("runtime_crash",    "medium",   "*"),      ["notify", "no_action"]),
    (("build_failure",    "high",     "high"),   ["trigger_retry"]),
    (("build_failure",    "*",        "*"),      ["notify", "no_action"]),
    (("dependency_error", "*",        "*"),      ["notify", "no_action"]),
    (("test_failure",     "*",        "*"),      ["notify", "no_action"]),
    (("unknown",          "*",        "*"),      ["no_action"]),
    (("*",                "*",        "*"),      ["notify", "no_action"]),  # global fallback
]


@dataclass
class DecisionResult:
    """Outcome of a policy check."""

    allowed: bool
    action: str    # final action (possibly overridden to no_action)
    original: str  # what the LLM originally proposed
    reason: str


def _matches(pattern: str, value: str) -> bool:
    """Return True when *pattern* is a wildcard or equals *value* exactly."""
    return pattern == "*" or pattern == value


def apply_policy(
    proposed_action: str,
    error_type: str,
    severity: str,
    confidence: str,
) -> DecisionResult:
    """Check *proposed_action* against ACTION_POLICY and return a DecisionResult.

    If the proposed action is in the allowed set for the matched row the result
    is ``allowed=True``.  Otherwise the action is overridden to the first item
    in the allowed set (usually ``no_action`` or ``notify``).
    """
    for (et, sev, conf), allowed in ACTION_POLICY:
        if _matches(et, error_type) and _matches(sev, severity) and _matches(conf, confidence):
            if proposed_action in allowed:
                logger.info({
                    "message": "policy_match",
                    "error_type": error_type,
                    "severity": severity,
                    "confidence": confidence,
                    "proposed": proposed_action,
                    "allowed": True,
                })
                return DecisionResult(
                    allowed=True,
                    action=proposed_action,
                    original=proposed_action,
                    reason=f"action '{proposed_action}' is allowed by policy for "
                           f"({error_type}, {severity}, {confidence})",
                )
            else:
                override = allowed[0]
                logger.info({
                    "message": "policy_override",
                    "error_type": error_type,
                    "severity": severity,
                    "confidence": confidence,
                    "proposed": proposed_action,
                    "override": override,
                })
                return DecisionResult(
                    allowed=False,
                    action=override,
                    original=proposed_action,
                    reason=f"action '{proposed_action}' not in allowed set {allowed} for "
                           f"({error_type}, {severity}, {confidence}); overriding to '{override}'",
                )

    # Should never reach here due to the global fallback row, but be safe.
    return DecisionResult(
        allowed=False,
        action="no_action",
        original=proposed_action,
        reason="no policy matched; defaulting to no_action",
    )

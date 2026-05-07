"""Policy engine: maps (error_type, severity, confidence) → allowed action set.

The policy table now lives in ``policy.yaml`` (loaded by ``app.core.policy``)
rather than being hardcoded here.  This module provides backwards-compatible
``ACTION_POLICY`` (rebuilt from the active policy) and the public
``apply_policy`` function.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from app.core.policy import get_policy
from app.utils.logger import get_logger

logger = get_logger("decision")


def _build_action_policy() -> List[Tuple[Tuple[str, str, str], List[str]]]:
    """Materialize the decision table into the legacy tuple-of-tuples format.

    Some tests still inspect ``ACTION_POLICY`` directly; keeping it compatible
    avoids breaking them.  Always rebuilt from the live policy on access.
    """
    return [
        ((row.match[0], row.match[1], row.match[2]), list(row.allowed))
        for row in get_policy().decision_table
    ]


# Backwards-compatibility shim — tests import this name.  It is a property-like
# global that reflects the live policy.  We rebuild on each module-level access
# in tests by calling _build_action_policy() — but since most tests only read
# this once at import, we materialize a snapshot here.  apply_policy() always
# reads the live policy.
ACTION_POLICY: List[Tuple[Tuple[str, str, str], List[str]]] = _build_action_policy()


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
    """Check *proposed_action* against the live policy and return a DecisionResult.

    The decision table is read from the active ``Policy`` object so that
    config-as-data updates (SIGHUP reloads) take effect immediately.
    """
    allowed = get_policy().lookup_decision(error_type, severity, confidence)
    if not allowed:
        return DecisionResult(
            allowed=False,
            action="no_action",
            original=proposed_action,
            reason="no policy matched; defaulting to no_action",
        )

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

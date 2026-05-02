"""Main safety controller for Phase 5.

Runs seven ordered checks between an LLM-proposed action and Kubernetes:

1. Causality gate        — unverified causality blocks destructive actions.
2. Decision policy       — ACTION_POLICY may override the action to no_action.
3. Loop detection        — repeated actions trigger escalation or freeze.
4. Idempotency           — recent identical actions in the same state are blocked.
5. Namespace isolation   — protected namespaces are never touched.
6. Severity gate         — destructive actions need high/critical severity + non-low confidence.
7. Rate limit            — restart_pod capped at 3 per service per 10 min.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.causality import CausalityResult
from app.core.decision import apply_policy
from app.core.loop_detector import check_loop
from app.services.memory_store import count_unresolved_actions, find_recent_actions
from app.utils.logger import get_logger

logger = get_logger("safety")

_SAFE_ACTIONS = {"notify", "no_action"}
_DESTRUCTIVE_ACTIONS = {"restart_pod", "rollback", "scale_up"}
_PROTECTED_NAMESPACES = {"kube-system", "monitoring", "kube-public"}
_RESTART_RATE_LIMIT = 3
_RESTART_RATE_WINDOW_MINUTES = 10


@dataclass
class SafetyResult:
    """Outcome of the full safety check chain."""

    allowed: bool
    action: str   # final action (may be overridden)
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)


async def validate(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    confidence: str,
    proposed_action: dict[str, Any],
    causality: CausalityResult,
) -> SafetyResult:
    """Run all safety checks and return a SafetyResult.

    Checks are short-circuit: the first denial stops further processing.
    The final *action* in the result may differ from the proposed action.
    """
    action_type: str = proposed_action.get("type", "no_action")
    namespace: str = proposed_action.get("namespace", "default")
    checks: dict[str, Any] = {}

    # ── 1. Causality gate ────────────────────────────────────────────────────
    if not causality.verified and action_type not in _SAFE_ACTIONS:
        checks["causality"] = "failed"
        logger.warning({
            "message": "safety_causality_gate_denied",
            "service": service,
            "action": action_type,
        })
        return SafetyResult(
            allowed=False,
            action="no_action",
            reason=f"causality not verified; blocking destructive action '{action_type}'",
            checks=checks,
        )
    checks["causality"] = "passed"

    # ── 2. Decision policy ───────────────────────────────────────────────────
    decision = apply_policy(action_type, error_type, severity, confidence)
    checks["policy"] = {"allowed": decision.allowed, "action": decision.action, "reason": decision.reason}
    action_type = decision.action  # may be overridden

    # ── 3. Loop detection ────────────────────────────────────────────────────
    loop = await check_loop(service, error_type)
    checks["loop"] = {"loop_detected": loop.loop_detected, "freeze": loop.freeze, "count": loop.count}

    if loop.freeze:
        logger.warning({
            "message": "safety_loop_freeze",
            "service": service,
            "reason": loop.reason,
        })
        return SafetyResult(
            allowed=False,
            action="no_action",
            reason=f"loop freeze: {loop.reason}",
            checks=checks,
        )

    if loop.loop_detected:
        # escalate to notify but don't block entirely
        logger.info({
            "message": "safety_loop_escalate",
            "service": service,
            "original_action": action_type,
        })
        action_type = "notify"
        checks["loop"]["escalated_to"] = "notify"

    # ── 4. Idempotency ───────────────────────────────────────────────────────
    recent = await find_recent_actions(
        service,
        action_type,
        states=["pending", "executing", "completed"],
        within_seconds=120,
    )
    checks["idempotency"] = {"recent_count": len(recent)}
    if recent:
        logger.info({
            "message": "safety_idempotency_denied",
            "service": service,
            "action": action_type,
            "recent": len(recent),
        })
        return SafetyResult(
            allowed=False,
            action="no_action",
            reason=f"idempotency: action '{action_type}' already executed {len(recent)} time(s) in the last 120s",
            checks=checks,
        )

    # ── 5. Namespace isolation ───────────────────────────────────────────────
    checks["namespace"] = namespace
    if namespace in _PROTECTED_NAMESPACES:
        logger.warning({
            "message": "safety_namespace_blocked",
            "service": service,
            "namespace": namespace,
        })
        return SafetyResult(
            allowed=False,
            action="no_action",
            reason=f"namespace isolation: namespace '{namespace}' is protected",
            checks=checks,
        )

    # ── 6. Severity gate ─────────────────────────────────────────────────────
    checks["severity_gate"] = {"action": action_type, "severity": severity, "confidence": confidence}
    if action_type in _DESTRUCTIVE_ACTIONS:
        if severity not in ("high", "critical") or confidence == "low":
            logger.warning({
                "message": "safety_severity_gate_denied",
                "service": service,
                "action": action_type,
                "severity": severity,
                "confidence": confidence,
            })
            return SafetyResult(
                allowed=False,
                action="no_action",
                reason=f"severity gate: destructive action '{action_type}' requires "
                       f"severity in {{high, critical}} and confidence != low "
                       f"(got severity='{severity}', confidence='{confidence}')",
                checks=checks,
            )

    # ── 7. Rate limit ────────────────────────────────────────────────────────
    if action_type == "restart_pod":
        rate_count = await count_unresolved_actions(
            service, error_type, window_minutes=_RESTART_RATE_WINDOW_MINUTES
        )
        checks["rate_limit"] = {"count": rate_count, "limit": _RESTART_RATE_LIMIT}
        if rate_count >= _RESTART_RATE_LIMIT:
            logger.warning({
                "message": "safety_rate_limit_denied",
                "service": service,
                "action": action_type,
                "count": rate_count,
            })
            return SafetyResult(
                allowed=False,
                action="no_action",
                reason=f"rate limit: restart_pod used {rate_count} times in the last "
                       f"{_RESTART_RATE_WINDOW_MINUTES} min (max {_RESTART_RATE_LIMIT})",
                checks=checks,
            )

    logger.info({
        "message": "safety_all_checks_passed",
        "service": service,
        "action": action_type,
    })
    return SafetyResult(
        allowed=True,
        action=action_type,
        reason="all safety checks passed",
        checks=checks,
    )

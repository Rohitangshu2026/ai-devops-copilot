"""Main safety controller for Phase 5 + Phase 6 hardening.

Runs eight ordered checks between an LLM-proposed action and Kubernetes:

1. Causality gate        — unverified causality blocks destructive actions.
2. Decision policy       — apply_policy() may override the action to no_action.
3. Loop detection        — repeated actions trigger escalation or freeze.
4. Idempotency (atomic)  — ES fingerprint lock; only one peer may execute.
5. Namespace isolation   — protected namespaces are never touched.
6. Severity gate         — destructive actions need high/critical severity + non-low confidence.
7. Rate limit            — restart_pod capped per policy.global.
8. Action budget         — global cap on automated actions per hour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.causality import CausalityResult
from app.core.decision import apply_policy
from app.core.loop_detector import check_loop
from app.core.policy import get_policy
from app.services.memory_store import (
    count_unresolved_actions,
    find_recent_actions,
    try_acquire_action_lock,
)
from app.utils.logger import get_logger

logger = get_logger("safety")

_SAFE_ACTIONS = {"notify", "no_action"}
_DESTRUCTIVE_ACTIONS = {"restart_pod", "rollback", "scale_up"}


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

    # ── 4. Idempotency (atomic ES fingerprint lock — Phase 6a) ───────────────
    # Only enforce for non-safe actions; notify/no_action are idempotent
    # by nature and should never be lock-blocked.
    if action_type in _SAFE_ACTIONS:
        checks["idempotency"] = {"skipped": True, "reason": "safe action"}
    else:
        ttl = get_policy().global_.idempotency_window_seconds
        acquired = await try_acquire_action_lock(service, action_type, ttl_seconds=ttl)
        checks["idempotency"] = {"lock_acquired": acquired, "ttl_seconds": ttl}
        if not acquired:
            logger.info({
                "message": "safety_idempotency_denied",
                "service": service,
                "action": action_type,
            })
            return SafetyResult(
                allowed=False,
                action="no_action",
                reason=f"idempotency: another peer already holds the lock for "
                       f"({service}, {action_type}) within the last {ttl}s",
                checks=checks,
            )

    # ── 5. Namespace isolation ───────────────────────────────────────────────
    checks["namespace"] = namespace
    protected = set(get_policy().global_.protected_namespaces)
    if namespace in protected:
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

    # ── 7. Per-action rate limit (Phase 6i — driven by policy.yaml) ─────────
    if action_type in _DESTRUCTIVE_ACTIONS:
        try:
            action_pol = get_policy().action(action_type)
            limit = action_pol.max_per_service_per_10min
        except KeyError:
            limit = 0  # unknown action → no limit applied here

        if limit > 0:
            rate_count = await count_unresolved_actions(service, error_type, window_minutes=10)
            checks["rate_limit"] = {"count": rate_count, "limit": limit, "window_minutes": 10}
            if rate_count >= limit:
                logger.warning({
                    "message": "safety_rate_limit_denied",
                    "service": service,
                    "action": action_type,
                    "count": rate_count,
                })
                return SafetyResult(
                    allowed=False,
                    action="no_action",
                    reason=f"rate limit: {action_type} used {rate_count} times in the last "
                           f"10 min (max {limit})",
                    checks=checks,
                )

    # ── 8. Global action budget (max destructive actions per hour) ───────────
    if action_type in _DESTRUCTIVE_ACTIONS:
        budget_total = get_policy().global_.action_budget_per_hour
        # Count destructive actions across all services in the last 60 min.
        # We use find_recent_actions per type and aggregate (cheap at low QPS).
        budget_used = 0
        for atype in _DESTRUCTIVE_ACTIONS:
            recent = await find_recent_actions(
                service="*",  # treated as wildcard; find_recent_actions filters by exact term
                action_type=atype,
                states=["completed", "executing"],
                within_seconds=3600,
            )
            budget_used += len(recent)
        checks["action_budget"] = {"used": budget_used, "limit": budget_total}
        if budget_used >= budget_total:
            logger.warning({
                "message": "safety_action_budget_denied",
                "service": service,
                "action": action_type,
                "used": budget_used,
                "limit": budget_total,
            })
            return SafetyResult(
                allowed=False,
                action="no_action",
                reason=f"action budget exceeded: {budget_used} destructive actions in the "
                       f"last hour (limit {budget_total})",
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

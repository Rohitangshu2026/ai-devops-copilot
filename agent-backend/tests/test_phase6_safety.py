"""Phase 6a/6i — safety controller integration tests for the new gates."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.causality import CausalityResult
from app.core.decision import DecisionResult
from app.core.loop_detector import LoopCheckResult


def _causality(verified: bool = True) -> CausalityResult:
    return CausalityResult(
        verified=verified,
        matched_evidence=["db"] if verified else [],
        target_redirected=False,
        action_target=None,
    )


def _loop_ok() -> LoopCheckResult:
    return LoopCheckResult(loop_detected=False, freeze=False, count=0, reason="no loop")


# ── Phase 6a — atomic idempotency gate ───────────────────────────────────────


class TestIdempotencyLock:
    @pytest.mark.asyncio
    async def test_lock_acquired_action_proceeds(self):
        """Lock acquired (returns True) → action proceeds through later gates."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_ok()
            mock_recent.return_value = []
            mock_count.return_value = 0
            mock_lock.return_value = True

            result = await safety_validate(
                service="my-service",
                environment="production",
                error_type="runtime_crash",
                severity="critical",
                confidence="high",
                proposed_action={"type": "restart_pod", "namespace": "default"},
                causality=_causality(verified=True),
            )

        assert result.allowed is True
        assert result.action == "restart_pod"
        # Lock was attempted exactly once
        mock_lock.assert_awaited_once()
        # The check appears in the result for auditability
        assert result.checks["idempotency"]["lock_acquired"] is True

    @pytest.mark.asyncio
    async def test_lock_conflict_blocks_action(self):
        """Lock returns False → safety denies with idempotency reason."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_ok()
            mock_recent.return_value = []
            mock_count.return_value = 0
            mock_lock.return_value = False  # peer beat us

            result = await safety_validate(
                service="my-service",
                environment="production",
                error_type="runtime_crash",
                severity="critical",
                confidence="high",
                proposed_action={"type": "restart_pod", "namespace": "default"},
                causality=_causality(verified=True),
            )

        assert result.allowed is False
        assert result.action == "no_action"
        assert "idempotency" in result.reason
        assert "another peer" in result.reason

    @pytest.mark.asyncio
    async def test_safe_actions_skip_lock(self):
        """notify and no_action don't need atomic locks (idempotent by nature)."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_ok()
            mock_recent.return_value = []
            mock_count.return_value = 0

            result = await safety_validate(
                service="my-service",
                environment="dev",
                error_type="dependency_error",
                severity="medium",
                confidence="medium",
                proposed_action={"type": "notify", "namespace": "default"},
                causality=_causality(verified=False),
            )

        # notify is safe → causality gate doesn't block, lock isn't called
        mock_lock.assert_not_called()
        assert result.checks["idempotency"]["skipped"] is True


# ── Phase 6i — policy-driven gates ───────────────────────────────────────────


class TestPolicyDrivenGates:
    @pytest.mark.asyncio
    async def test_protected_namespace_from_policy(self):
        """Namespace blocklist is read from policy.global_.protected_namespaces."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_ok()
            mock_recent.return_value = []
            mock_count.return_value = 0
            mock_lock.return_value = True

            result = await safety_validate(
                service="my-service",
                environment="production",
                error_type="runtime_crash",
                severity="critical",
                confidence="high",
                proposed_action={"type": "restart_pod", "namespace": "kube-system"},
                causality=_causality(verified=True),
            )

        assert result.allowed is False
        assert "namespace" in result.reason

    @pytest.mark.asyncio
    async def test_action_budget_blocks_when_exceeded(self):
        """When destructive actions in last hour >= policy.global_.action_budget_per_hour, deny."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_ok()
            # Action budget gate counts across all destructive types — return
            # 6 entries each → total 18 (default budget = 5).
            mock_recent.return_value = [{}] * 6
            mock_count.return_value = 0
            mock_lock.return_value = True

            result = await safety_validate(
                service="my-service",
                environment="production",
                error_type="runtime_crash",
                severity="critical",
                confidence="high",
                proposed_action={"type": "restart_pod", "namespace": "default"},
                causality=_causality(verified=True),
            )

        assert result.allowed is False
        assert "action budget" in result.reason

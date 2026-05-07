"""Tests for Phase 5 safety stack: decision policy, loop detector, safety controller."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.causality import CausalityResult
from app.core.decision import DecisionResult, apply_policy
from app.core.loop_detector import LoopCheckResult, check_loop


# ── Decision policy tests ─────────────────────────────────────────────────────

class TestDecisionPolicy:
    def test_decision_policy_runtime_crash_critical(self):
        """runtime_crash + critical + high confidence → restart_pod allowed."""
        result = apply_policy("restart_pod", "runtime_crash", "critical", "high")
        assert result.allowed is True
        assert result.action == "restart_pod"

    def test_decision_policy_unknown(self):
        """unknown error type → only no_action allowed, restart_pod overridden."""
        result = apply_policy("restart_pod", "unknown", "critical", "high")
        assert result.allowed is False
        assert result.action == "no_action"
        assert result.original == "restart_pod"

    def test_decision_policy_dependency_error(self):
        """dependency_error → only notify/no_action allowed."""
        result_notify = apply_policy("notify", "dependency_error", "critical", "high")
        assert result_notify.allowed is True
        assert result_notify.action == "notify"

        result_restart = apply_policy("restart_pod", "dependency_error", "high", "high")
        assert result_restart.allowed is False
        assert result_restart.action in ("notify", "no_action")

    def test_decision_policy_override(self):
        """runtime_crash + medium severity + any confidence → restart_pod overridden."""
        result = apply_policy("restart_pod", "runtime_crash", "medium", "low")
        assert result.allowed is False
        assert result.action in ("notify", "no_action")
        assert result.original == "restart_pod"

    def test_decision_policy_build_failure_high_high(self):
        """build_failure + high + high → trigger_retry allowed."""
        result = apply_policy("trigger_retry", "build_failure", "high", "high")
        assert result.allowed is True
        assert result.action == "trigger_retry"

    def test_decision_policy_runtime_crash_high_medium(self):
        """runtime_crash + high + medium → only notify allowed."""
        result = apply_policy("notify", "runtime_crash", "high", "medium")
        assert result.allowed is True
        assert result.action == "notify"

        result2 = apply_policy("restart_pod", "runtime_crash", "high", "medium")
        assert result2.allowed is False
        assert result2.action == "notify"

    def test_decision_policy_global_fallback(self):
        """Unrecognised error type falls through to global wildcard row."""
        result = apply_policy("no_action", "some_new_error", "medium", "medium")
        assert result.allowed is True
        assert result.action == "no_action"


# ── Loop detector tests ───────────────────────────────────────────────────────

class TestLoopDetector:
    @pytest.mark.asyncio
    async def test_loop_detector_ok(self):
        """count=0 → no loop detected."""
        with (
            patch("app.core.loop_detector.is_service_frozen", new_callable=AsyncMock) as mock_frozen,
            patch("app.core.loop_detector.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
        ):
            mock_frozen.return_value = False
            mock_count.return_value = 0

            result = await check_loop("my-service", "runtime_crash")

        assert result.loop_detected is False
        assert result.freeze is False
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_loop_detector_escalate(self):
        """count=3 → loop_detected=True, freeze=False."""
        with (
            patch("app.core.loop_detector.is_service_frozen", new_callable=AsyncMock) as mock_frozen,
            patch("app.core.loop_detector.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
        ):
            mock_frozen.return_value = False
            mock_count.return_value = 3

            result = await check_loop("my-service", "runtime_crash")

        assert result.loop_detected is True
        assert result.freeze is False
        assert result.count == 3

    @pytest.mark.asyncio
    async def test_loop_detector_freeze(self):
        """count=5 → freeze=True."""
        with (
            patch("app.core.loop_detector.is_service_frozen", new_callable=AsyncMock) as mock_frozen,
            patch("app.core.loop_detector.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
        ):
            mock_frozen.return_value = False
            mock_count.return_value = 5

            result = await check_loop("my-service", "runtime_crash")

        assert result.freeze is True
        assert result.loop_detected is True
        assert result.count == 5

    @pytest.mark.asyncio
    async def test_loop_detector_frozen_service(self):
        """is_service_frozen=True → freeze=True, count=-1."""
        with (
            patch("app.core.loop_detector.is_service_frozen", new_callable=AsyncMock) as mock_frozen,
            patch("app.core.loop_detector.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
        ):
            mock_frozen.return_value = True
            mock_count.return_value = 0  # should not be called

            result = await check_loop("frozen-service", "runtime_crash")

        assert result.freeze is True
        assert result.count == -1
        mock_count.assert_not_called()


# ── Safety controller tests ───────────────────────────────────────────────────

def _causality(verified: bool = True) -> CausalityResult:
    return CausalityResult(
        verified=verified,
        matched_evidence=["db"] if verified else [],
        target_redirected=False,
        action_target=None,
    )


def _loop_ok() -> LoopCheckResult:
    return LoopCheckResult(loop_detected=False, freeze=False, count=0, reason="no loop")


def _loop_escalate() -> LoopCheckResult:
    return LoopCheckResult(loop_detected=True, freeze=False, count=3, reason="escalate")


def _loop_freeze() -> LoopCheckResult:
    return LoopCheckResult(loop_detected=True, freeze=True, count=5, reason="freeze")


class TestSafetyController:
    @pytest.mark.asyncio
    async def test_safety_causality_gate_blocks_destructive(self):
        """causality.verified=False + restart_pod → denied."""
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
                causality=_causality(verified=False),
            )

        assert result.allowed is False
        assert result.action == "no_action"
        assert "causality" in result.reason

    @pytest.mark.asyncio
    async def test_safety_causality_gate_allows_notify(self):
        """causality.verified=False + notify → allowed (notify is safe)."""
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
                error_type="dependency_error",
                severity="high",
                confidence="medium",
                proposed_action={"type": "notify", "namespace": "default"},
                causality=_causality(verified=False),
            )

        assert result.action == "notify"

    @pytest.mark.asyncio
    async def test_safety_severity_gate(self):
        """restart_pod with low confidence on a high-severity crash → severity gate denies.

        The policy for runtime_crash+critical allows restart_pod only when confidence=high.
        We bypass policy by mocking apply_policy to return the action as allowed, then let
        the severity gate enforce confidence!=low.
        """
        from app.core.safety import validate as safety_validate

        # Patch apply_policy to simulate a policy gap (action "allowed" by policy)
        # so the severity gate fires when confidence=low.
        allowed_decision = DecisionResult(
            allowed=True,
            action="restart_pod",
            original="restart_pod",
            reason="policy allowed (mocked)",
        )

        with (
            patch("app.core.safety.apply_policy", return_value=allowed_decision),
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
                severity="low",
                confidence="low",
                proposed_action={"type": "restart_pod", "namespace": "default"},
                causality=_causality(verified=True),
            )

        assert result.allowed is False
        assert result.action == "no_action"
        assert "severity gate" in result.reason

    @pytest.mark.asyncio
    async def test_safety_namespace_blocked(self):
        """target namespace=kube-system → denied."""
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
    async def test_safety_loop_escalates_action_to_notify(self):
        """loop count=3 (escalate) + proposed restart_pod → final action=notify."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_escalate()
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

        assert result.action == "notify"

    @pytest.mark.asyncio
    async def test_safety_loop_freeze_blocks(self):
        """loop freeze → denied."""
        from app.core.safety import validate as safety_validate

        with (
            patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
            patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
            patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
            patch("app.core.safety.try_acquire_action_lock", new_callable=AsyncMock) as mock_lock,
        ):
            mock_loop.return_value = _loop_freeze()
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

        assert result.allowed is False
        assert result.action == "no_action"

    @pytest.mark.asyncio
    async def test_safety_all_checks_pass(self):
        """Happy path: all checks pass."""
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

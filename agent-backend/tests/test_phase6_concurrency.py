"""Phase 6 — adversarial concurrency tests.

Verifies the safety stack does what we claim under simulated load:
  * 50 concurrent analyze-equivalent safety validations on the same
    (service, action) → exactly one passes the idempotency gate.
  * 50 concurrent lease acquisitions → exactly one winner.
  * Sweeper + multiple concurrent verifications → no double-processing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.causality import CausalityResult
from app.core.loop_detector import LoopCheckResult
from app.services.memory_store import try_acquire_action_lock, try_acquire_lease


def _causality_ok():
    return CausalityResult(verified=True, matched_evidence=["x"],
                           target_redirected=False, action_target=None)


def _loop_ok():
    return LoopCheckResult(loop_detected=False, freeze=False, count=0, reason="ok")


@pytest.mark.asyncio
async def test_50_concurrent_safety_validates_single_winner():
    """50 parallel calls to safety.validate against the same (service, action)
    must result in exactly one allowed=True.  The idempotency lock is the
    only thing that prevents the rest from passing."""
    from app.core.safety import validate as safety_validate

    # Track lock acquisitions: first wins, rest get conflict.
    call_count = {"n": 0}

    async def mock_acquire(service, action_type, ttl_seconds=120):
        call_count["n"] += 1
        # Only the very first caller wins.
        return call_count["n"] == 1

    with (
        patch("app.core.safety.check_loop", new_callable=AsyncMock) as mock_loop,
        patch("app.core.safety.find_recent_actions", new_callable=AsyncMock) as mock_recent,
        patch("app.core.safety.count_unresolved_actions", new_callable=AsyncMock) as mock_count,
        patch("app.core.safety.try_acquire_action_lock", side_effect=mock_acquire),
    ):
        mock_loop.return_value = _loop_ok()
        mock_recent.return_value = []
        mock_count.return_value = 0

        results = await asyncio.gather(*[
            safety_validate(
                service="contested-service",
                environment="production",
                error_type="runtime_crash",
                severity="critical",
                confidence="high",
                proposed_action={"type": "restart_pod", "namespace": "default"},
                causality=_causality_ok(),
            )
            for _ in range(50)
        ])

    allowed = [r for r in results if r.allowed]
    assert len(allowed) == 1, (
        f"expected exactly 1 allowed action, got {len(allowed)}"
    )

    # The 49 losers all give the idempotency reason.
    losers = [r for r in results if not r.allowed]
    assert len(losers) == 49
    for r in losers:
        assert "idempotency" in r.reason


@pytest.mark.asyncio
async def test_50_concurrent_lease_acquisitions_single_winner():
    """At ES level, op_type=create gives exactly one winner across N parallel calls."""
    client = MagicMock()
    call_count = {"n": 0}

    async def mock_index(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"_id": "first"}
        raise Exception("version_conflict_engine_exception: 409")

    client.index = AsyncMock(side_effect=mock_index)

    with patch("app.services.memory_store.get_client", return_value=client):
        results = await asyncio.gather(*[
            try_acquire_lease(f"contested-lease") for _ in range(50)
        ])

    assert sum(results) == 1
    assert results.count(True) == 1
    assert results.count(False) == 49


@pytest.mark.asyncio
async def test_concurrent_action_locks_per_minute_bucket():
    """Lock id is keyed on a 60s minute bucket — same minute → same id."""
    from app.services.memory_store import _action_lock_id

    ids = {_action_lock_id("svc", "restart_pod") for _ in range(100)}
    # All 100 calls within the same minute produce the same lock id.
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_lease_renewal_under_contention():
    """Renewal should fail (return False) when the lease was stolen."""
    from app.services.memory_store import renew_lease

    client = MagicMock()
    # The painless script returns 'noop' when owner check fails.
    client.update = AsyncMock(return_value={"result": "noop"})

    with patch("app.services.memory_store.get_client", return_value=client):
        results = await asyncio.gather(*[
            renew_lease("lease-1", owner="rightful-owner")
            for _ in range(20)
        ])

    # All renewals fail because the script says 'noop' (wrong owner).
    assert all(r is False for r in results)


@pytest.mark.asyncio
async def test_sweeper_no_double_processing_under_concurrency():
    """If the sweeper is invoked concurrently in two pods, lease ensures
    only one of them processes a given verification job."""
    from app.core.impact import _process_one_verification

    # First call wins the lease, second is rejected.
    call_count = {"n": 0}

    async def mock_lease(*args, **kwargs):
        call_count["n"] += 1
        return call_count["n"] == 1

    with (
        patch("app.core.impact.try_acquire_lease", side_effect=mock_lease),
        patch("app.core.impact.release_lease", new_callable=AsyncMock),
        patch("app.core.impact.verify_resolution",
              new_callable=AsyncMock) as mock_verify,
        patch("app.core.impact.update_incident", new_callable=AsyncMock),
        patch("app.core.impact.delete_verification_job", new_callable=AsyncMock),
    ):
        mock_verify.return_value = "resolved"

        job = {"incident_id": "inc-1", "service": "svc",
               "environment": "dev", "baseline_error_ratio": 0.5}

        # Two pods both pick up the same job.
        await asyncio.gather(
            _process_one_verification(job),
            _process_one_verification(job),
        )

    # Verification ran exactly once.
    assert mock_verify.await_count == 1

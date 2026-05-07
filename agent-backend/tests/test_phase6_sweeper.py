"""Phase 6b — verification sweeper tests.

Verifies the persistent verifier does not lose jobs across restarts and
processes due jobs under a lease.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.impact as impact_mod
from app.core.impact import (
    _process_one_verification,
    run_verification_sweeper,
    schedule_verification,
)


class TestScheduleVerification:
    @pytest.mark.asyncio
    async def test_schedule_uses_persistent_queue(self):
        """schedule_verification must call enqueue_verification (persistent), not asyncio.create_task."""
        with patch(
            "app.core.impact.enqueue_verification", new_callable=AsyncMock
        ) as mock_enqueue:
            mock_enqueue.return_value = True
            await schedule_verification(
                incident_id="inc-1",
                service="svc",
                environment="dev",
                baseline_error_ratio=0.5,
                delay_seconds=120,
            )
        mock_enqueue.assert_awaited_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["incident_id"] == "inc-1"
        assert kwargs["service"] == "svc"
        assert kwargs["delay_seconds"] == 120

    @pytest.mark.asyncio
    async def test_schedule_falls_back_to_asyncio_when_es_down(self):
        """If enqueue fails, the legacy in-memory task path should still run."""
        spawned = []
        original_create = asyncio.create_task

        def tracking_create(coro, *a, **kw):
            t = original_create(coro, *a, **kw)
            spawned.append(t)
            return t

        with (
            patch("app.core.impact.enqueue_verification",
                  new_callable=AsyncMock) as mock_enqueue,
            patch("app.core.impact.asyncio.create_task", side_effect=tracking_create),
        ):
            mock_enqueue.return_value = False
            await schedule_verification(
                incident_id="inc-1",
                service="svc",
                environment="dev",
                baseline_error_ratio=0.5,
                delay_seconds=0,
            )

        # A background task should have been spawned as fallback.
        assert len(spawned) >= 1
        # Cancel to keep test runner clean.
        for t in spawned:
            t.cancel()


class TestProcessOneVerification:
    @pytest.mark.asyncio
    async def test_lease_taken_aborts(self):
        """When the lease cannot be acquired, the worker must abort silently."""
        with (
            patch("app.core.impact.try_acquire_lease",
                  new_callable=AsyncMock) as mock_acq,
            patch("app.core.impact.verify_resolution",
                  new_callable=AsyncMock) as mock_verify,
            patch("app.core.impact.update_incident",
                  new_callable=AsyncMock) as mock_update,
            patch("app.core.impact.delete_verification_job",
                  new_callable=AsyncMock) as mock_delete,
        ):
            mock_acq.return_value = False
            await _process_one_verification(
                {"incident_id": "inc-1", "service": "svc",
                 "environment": "dev", "baseline_error_ratio": 0.5}
            )
        mock_verify.assert_not_called()
        mock_update.assert_not_called()
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_verifies_updates_deletes(self):
        with (
            patch("app.core.impact.try_acquire_lease",
                  new_callable=AsyncMock) as mock_acq,
            patch("app.core.impact.release_lease",
                  new_callable=AsyncMock),
            patch("app.core.impact.verify_resolution",
                  new_callable=AsyncMock) as mock_verify,
            patch("app.core.impact.update_incident",
                  new_callable=AsyncMock) as mock_update,
            patch("app.core.impact.delete_verification_job",
                  new_callable=AsyncMock) as mock_delete,
        ):
            mock_acq.return_value = True
            mock_verify.return_value = "resolved"
            await _process_one_verification(
                {"incident_id": "inc-1", "service": "svc",
                 "environment": "dev", "baseline_error_ratio": 0.5}
            )
        mock_verify.assert_awaited_once()
        mock_update.assert_awaited_once()
        # Outcome flowed through to update.
        update_kwargs = mock_update.call_args.args[1]
        assert update_kwargs["outcome"] == "resolved"
        mock_delete.assert_awaited_once_with("inc-1")

    @pytest.mark.asyncio
    async def test_missing_incident_id_does_nothing(self):
        with (
            patch("app.core.impact.try_acquire_lease",
                  new_callable=AsyncMock) as mock_acq,
            patch("app.core.impact.verify_resolution",
                  new_callable=AsyncMock) as mock_verify,
        ):
            await _process_one_verification({"service": "svc"})
        mock_acq.assert_not_called()
        mock_verify.assert_not_called()


class TestSweeperLoop:
    @pytest.mark.asyncio
    async def test_sweeper_processes_due_jobs_and_stops_on_event(self):
        """One iteration: claim jobs, process them, sleep — then stop_event fires."""
        # Make the sweep interval tiny so the test is fast.
        original_interval = impact_mod._SWEEP_INTERVAL_SECONDS
        impact_mod._SWEEP_INTERVAL_SECONDS = 0.05

        try:
            jobs = [
                {"incident_id": "inc-1", "service": "svc",
                 "environment": "dev", "baseline_error_ratio": 0.5}
            ]

            with (
                patch("app.core.impact.claim_due_verifications",
                      new_callable=AsyncMock) as mock_claim,
                patch("app.core.impact._process_one_verification",
                      new_callable=AsyncMock) as mock_proc,
            ):
                # Return jobs once, then empty.
                mock_claim.side_effect = [jobs, []]

                stop = asyncio.Event()
                task = asyncio.create_task(run_verification_sweeper(stop))

                # Let it run for a few sweep intervals.
                await asyncio.sleep(0.2)
                stop.set()
                await asyncio.wait_for(task, timeout=2.0)

            assert mock_proc.await_count >= 1
        finally:
            impact_mod._SWEEP_INTERVAL_SECONDS = original_interval

    @pytest.mark.asyncio
    async def test_sweeper_recovers_from_claim_error(self):
        """An exception in claim_due must not crash the sweeper."""
        original_interval = impact_mod._SWEEP_INTERVAL_SECONDS
        impact_mod._SWEEP_INTERVAL_SECONDS = 0.05

        try:
            with (
                patch("app.core.impact.claim_due_verifications",
                      new_callable=AsyncMock) as mock_claim,
            ):
                mock_claim.side_effect = [
                    Exception("ES down"),
                    [],   # recovers next iteration
                    [],
                ]

                stop = asyncio.Event()
                task = asyncio.create_task(run_verification_sweeper(stop))
                await asyncio.sleep(0.2)
                stop.set()
                await asyncio.wait_for(task, timeout=2.0)

            # Sweeper kept running across the error.
            assert mock_claim.await_count >= 2
        finally:
            impact_mod._SWEEP_INTERVAL_SECONDS = original_interval

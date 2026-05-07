"""Phase 6e/6f — async execution + snapshot rollback tests."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.action_executor import (
    ExecutionResult,
    _BACKGROUND_TASKS,
    execute,
    execute_async,
)
from app.core.rollback import (
    RollbackEntry,
    _clear_snapshots,
    _strip_runtime_fields,
    capture_snapshot,
    get_snapshot,
    rollback,
)


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset in-memory snapshots between tests."""
    _clear_snapshots()
    _BACKGROUND_TASKS.clear()
    yield
    _clear_snapshots()
    _BACKGROUND_TASKS.clear()


# ── Phase 6e — snapshot rollback ─────────────────────────────────────────────


class TestSnapshotCapture:
    @pytest.mark.asyncio
    async def test_capture_when_kubectl_returns_spec(self):
        spec = {
            "spec": {"replicas": 3, "template": {"spec": {"containers": [{"image": "img:v1"}]}}},
            "metadata": {"name": "svc"},
        }
        with patch("app.core.rollback._run_kubectl", return_value=json.dumps(spec)):
            entry = await capture_snapshot("act-1", "svc", "default")
        assert entry is not None
        assert entry.previous_image == "img:v1"
        assert entry.previous_replicas == 3
        assert entry.raw_spec is not None
        # Stored under action_id
        assert get_snapshot("act-1") is entry

    @pytest.mark.asyncio
    async def test_capture_returns_none_when_kubectl_unavailable(self):
        with patch("app.core.rollback._run_kubectl", return_value=None):
            entry = await capture_snapshot("act-1", "svc")
        assert entry is None
        assert get_snapshot("act-1") is None

    @pytest.mark.asyncio
    async def test_capture_handles_invalid_json(self):
        with patch("app.core.rollback._run_kubectl", return_value="<not json>"):
            entry = await capture_snapshot("act-1", "svc")
        assert entry is None


class TestStripRuntimeFields:
    def test_strips_status(self):
        spec = {"spec": {}, "status": {"readyReplicas": 3}}
        out = _strip_runtime_fields(spec)
        assert "status" not in out

    def test_strips_metadata_runtime_fields(self):
        spec = {
            "metadata": {
                "name": "svc",
                "resourceVersion": "12345",
                "uid": "abc-def",
                "generation": 5,
                "managedFields": [{"manager": "kubectl"}],
                "creationTimestamp": "2024-01-01T00:00:00Z",
                "annotations": {"deployment.kubernetes.io/revision": "3", "user.example.com/foo": "bar"},
            },
            "spec": {},
        }
        out = _strip_runtime_fields(spec)
        m = out["metadata"]
        assert "resourceVersion" not in m
        assert "uid" not in m
        assert "generation" not in m
        assert "managedFields" not in m
        assert "creationTimestamp" not in m
        assert "deployment.kubernetes.io/revision" not in m["annotations"]
        # User annotations preserved
        assert m["annotations"]["user.example.com/foo"] == "bar"
        # Name preserved
        assert m["name"] == "svc"


class TestRollbackFromSnapshot:
    @pytest.mark.asyncio
    async def test_rollback_uses_snapshot_apply_when_available(self):
        """Phase 6e — rollback prefers `kubectl apply -f -` over `rollout undo`."""
        # Capture a snapshot first.
        spec = {
            "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": "img:v1"}]}}},
            "metadata": {"name": "svc"},
        }
        with patch("app.core.rollback._run_kubectl", return_value=json.dumps(spec)):
            await capture_snapshot("act-1", "svc")

        # Now rollback. _run_kubectl should be called with `apply -f -`,
        # NOT with `rollout undo`.
        calls = []
        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return "ok"

        async def _ready_ok(*args, **kwargs):
            return True

        with (
            patch("app.core.rollback._run_kubectl", side_effect=fake_run),
            patch("app.core.rollback._wait_for_running", side_effect=_ready_ok),
        ):
            ok = await rollback("act-1", "svc")

        assert ok is True
        # First call must include 'apply' as first positional arg
        first = calls[0][0]
        assert "apply" in first
        assert "rollout" not in first or "undo" not in first

    @pytest.mark.asyncio
    async def test_rollback_falls_back_to_rollout_undo_when_no_snapshot(self):
        """When no snapshot was captured, fall back to rollout undo."""
        calls = []
        def fake_run(*args, **kwargs):
            calls.append(args)
            return "ok"

        async def _ready_ok(*args, **kwargs):
            return True

        with (
            patch("app.core.rollback._run_kubectl", side_effect=fake_run),
            patch("app.core.rollback._wait_for_running", side_effect=_ready_ok),
        ):
            ok = await rollback("nonexistent-act", "svc")
        assert ok is True
        assert "rollout" in calls[0]
        assert "undo" in calls[0]

    @pytest.mark.asyncio
    async def test_rollback_returns_false_when_apply_fails(self):
        """Phase 6e — terminal failure: rollback apply itself fails."""
        spec = {
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "img:v1"}]}}},
        }
        with patch("app.core.rollback._run_kubectl", return_value=json.dumps(spec)):
            await capture_snapshot("act-1", "svc")

        with patch("app.core.rollback._run_kubectl", return_value=None):
            ok = await rollback("act-1", "svc")
        assert ok is False

    @pytest.mark.asyncio
    async def test_rollback_returns_false_when_pod_not_ready(self):
        """Phase 6e — apply succeeds but pod never becomes ready → terminal failure."""
        spec = {
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "img:v1"}]}}},
        }
        with patch("app.core.rollback._run_kubectl", return_value=json.dumps(spec)):
            await capture_snapshot("act-1", "svc")

        async def _never_ready(*args, **kwargs):
            return False

        with (
            patch("app.core.rollback._run_kubectl", return_value="ok"),
            patch("app.core.rollback._wait_for_running", side_effect=_never_ready),
        ):
            ok = await rollback("act-1", "svc")
        assert ok is False


# ── Phase 6f — async action execution ────────────────────────────────────────


class TestExecuteAsync:
    @pytest.mark.asyncio
    async def test_noop_action_returns_skipped_synchronously(self):
        result = await execute_async("act-1", "notify", "svc")
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_dry_run_returns_dry_run_ok_synchronously(self):
        with (
            patch("app.core.action_executor._kubectl_available", return_value=True),
            patch(
                "app.core.action_executor._run_kubectl",
                return_value=(0, "ok", ""),
            ),
        ):
            result = await execute_async("act-1", "restart_pod", "svc", dry_run=True)
        assert result.status == "dry_run_ok"

    @pytest.mark.asyncio
    async def test_kubectl_unavailable_returns_skipped(self):
        with patch("app.core.action_executor._kubectl_available", return_value=False):
            result = await execute_async("act-1", "restart_pod", "svc")
        assert result.status == "skipped"
        assert result.error == "kubectl not available"

    @pytest.mark.asyncio
    async def test_dry_run_failure_returns_failed_synchronously(self):
        with (
            patch("app.core.action_executor._kubectl_available", return_value=True),
            patch("app.core.action_executor._run_kubectl",
                  return_value=(1, "", "validation error")),
        ):
            result = await execute_async("act-1", "restart_pod", "svc")
        assert result.status == "failed"
        assert "dry-run failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_returns_executing_immediately_then_polls_in_background(self):
        """Phase 6f — async should return executing without waiting for poll."""
        async def _ok_lock(*args, **kwargs):
            return True

        async def _ok_renew(*args, **kwargs):
            return True

        async def _ok_release(*args, **kwargs):
            return None

        async def _ok_capture(*args, **kwargs):
            from app.core.rollback import RollbackEntry
            return RollbackEntry(
                action_id="act-1", service="svc", previous_image="img",
                previous_replicas=1, spec_hash="abc", captured_at="now",
            )

        async def _slow_poll(*args, **kwargs):
            await asyncio.sleep(0.05)
            return 1, 1

        # Keep patches active until the background task completes.
        with (
            patch("app.core.action_executor._kubectl_available", return_value=True),
            patch("app.core.action_executor._run_kubectl", return_value=(0, "ok", "")),
            patch("app.core.action_executor.try_acquire_lease", side_effect=_ok_lock),
            patch("app.core.action_executor.release_lease", side_effect=_ok_release),
            patch("app.core.action_executor.renew_lease", side_effect=_ok_renew),
            patch("app.core.action_executor.capture_snapshot", side_effect=_ok_capture),
            patch("app.core.action_executor._poll_ready", side_effect=_slow_poll),
            patch("app.core.action_executor.update_incident", new_callable=AsyncMock),
        ):
            import time
            start = time.monotonic()
            result = await execute_async(
                "act-1", "restart_pod", "svc", incident_id="inc-1"
            )
            elapsed = time.monotonic() - start

            # Returned well under the simulated poll latency.
            assert elapsed < 0.5
            assert result.status == "executing"
            assert "act-1" in _BACKGROUND_TASKS

            # Wait for the background task INSIDE the patch context so the
            # mocked dependencies remain valid until completion.
            task = _BACKGROUND_TASKS.get("act-1")
            if task:
                await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_lease_already_held_returns_executing_no_double_poll(self):
        """If another pod holds the lease, do not start a 2nd poll loop."""
        async def _no_lock(*args, **kwargs):
            return False

        async def _ok_capture(*args, **kwargs):
            from app.core.rollback import RollbackEntry
            return RollbackEntry(
                action_id="act-1", service="svc", previous_image="img",
                previous_replicas=1, spec_hash="abc", captured_at="now",
            )

        with (
            patch("app.core.action_executor._kubectl_available", return_value=True),
            patch("app.core.action_executor._run_kubectl", return_value=(0, "ok", "")),
            patch("app.core.action_executor.try_acquire_lease", side_effect=_no_lock),
            patch("app.core.action_executor.capture_snapshot", side_effect=_ok_capture),
        ):
            result = await execute_async("act-1", "restart_pod", "svc")

        assert result.status == "executing"
        # No background task spawned because the lease was already held.
        assert "act-1" not in _BACKGROUND_TASKS


class TestExecuteSync:
    """The legacy synchronous execute() path is still in active use by tests
    and the agent pipeline.  Verify it still works correctly."""

    @pytest.mark.asyncio
    async def test_sync_execute_noop(self):
        result = await execute("act-1", "notify", "svc")
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_sync_execute_kubectl_unavailable(self):
        with patch("app.core.action_executor._kubectl_available", return_value=False):
            result = await execute("act-1", "restart_pod", "svc")
        assert result.status == "skipped"

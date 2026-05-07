"""Phase 6a/b/c/h — persistence layer tests.

Covers:
  6a — try_acquire_action_lock atomic op_type=create + 409 conflict path
  6b — enqueue/claim/delete_verification + sweeper loop
  6c — daily index pattern + ILM bootstrap
  6h — try_acquire_lease, renew_lease, release_lease, recover_orphaned_leases

These tests use mocked ES so they run without a live cluster.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.memory_store import (
    _action_lock_id,
    _hostname,
    _write_index,
    bootstrap_ilm_policy,
    claim_due_verifications,
    delete_verification_job,
    enqueue_verification,
    recover_orphaned_leases,
    release_lease,
    renew_lease,
    save_incident,
    try_acquire_action_lock,
    try_acquire_lease,
)


def _es_conflict_error():
    """Return an exception that looks like an ES 409 Conflict."""
    return Exception("version_conflict_engine_exception: 409")


def _es_other_error():
    return Exception("connection refused: cluster unavailable")


# ── Phase 6a — atomic idempotency ────────────────────────────────────────────


class TestActionLock:
    def test_lock_id_deterministic_within_minute(self):
        a = _action_lock_id("svc", "restart_pod")
        b = _action_lock_id("svc", "restart_pod")
        assert a == b

    def test_lock_id_differs_per_action(self):
        a = _action_lock_id("svc", "restart_pod")
        b = _action_lock_id("svc", "rollback")
        assert a != b

    def test_lock_id_differs_per_service(self):
        a = _action_lock_id("svc1", "restart_pod")
        b = _action_lock_id("svc2", "restart_pod")
        assert a != b

    @pytest.mark.asyncio
    async def test_acquire_lock_first_winner(self):
        client = MagicMock()
        client.index = AsyncMock(return_value={"_id": "abc"})
        with patch("app.services.memory_store.get_client", return_value=client):
            won = await try_acquire_action_lock("svc-x", "restart_pod")
        assert won is True
        # Verify op_type=create was used
        kwargs = client.index.call_args.kwargs
        assert kwargs["op_type"] == "create"

    @pytest.mark.asyncio
    async def test_acquire_lock_conflict_returns_false(self):
        client = MagicMock()
        client.index = AsyncMock(side_effect=_es_conflict_error())
        with patch("app.services.memory_store.get_client", return_value=client):
            won = await try_acquire_action_lock("svc-x", "restart_pod")
        assert won is False

    @pytest.mark.asyncio
    async def test_acquire_lock_other_error_fails_open(self):
        """Non-conflict ES errors should not block the pipeline."""
        client = MagicMock()
        client.index = AsyncMock(side_effect=_es_other_error())
        with patch("app.services.memory_store.get_client", return_value=client):
            won = await try_acquire_action_lock("svc-x", "restart_pod")
        # Fail-open: returns True so the caller proceeds.
        assert won is True

    @pytest.mark.asyncio
    async def test_concurrent_acquire_only_one_wins(self):
        """Simulate 50 concurrent calls; mock ES so the first call succeeds
        and all subsequent calls receive 409 Conflict — exactly one True."""
        client = MagicMock()
        call_count = {"n": 0}

        async def mock_index(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"_id": "first"}
            raise _es_conflict_error()

        client.index = AsyncMock(side_effect=mock_index)
        with patch("app.services.memory_store.get_client", return_value=client):
            results = await asyncio.gather(*[
                try_acquire_action_lock("svc-c", "restart_pod") for _ in range(50)
            ])
        assert sum(results) == 1


# ── Phase 6b — persistent verification queue ─────────────────────────────────


class TestVerificationQueue:
    @pytest.mark.asyncio
    async def test_enqueue_verification_writes_doc(self):
        client = MagicMock()
        client.index = AsyncMock(return_value={"_id": "inc-1"})
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await enqueue_verification(
                incident_id="inc-1",
                service="svc",
                environment="dev",
                baseline_error_ratio=0.5,
                delay_seconds=120,
            )
        assert ok is True
        kwargs = client.index.call_args.kwargs
        assert kwargs["index"] == "devops-pending-verifications"
        assert kwargs["id"] == "inc-1"
        doc = kwargs["document"]
        assert doc["service"] == "svc"
        assert "verify_after_unix" in doc

    @pytest.mark.asyncio
    async def test_enqueue_failure_returns_false(self):
        client = MagicMock()
        client.index = AsyncMock(side_effect=_es_other_error())
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await enqueue_verification("inc-1", "svc", "dev", 0.5, 120)
        assert ok is False

    @pytest.mark.asyncio
    async def test_claim_due_returns_overdue_jobs(self):
        client = MagicMock()
        client.search = AsyncMock(return_value={
            "hits": {"hits": [
                {"_id": "inc-1", "_source": {"incident_id": "inc-1", "service": "svc"}},
                {"_id": "inc-2", "_source": {"incident_id": "inc-2", "service": "svc"}},
            ]}
        })
        with patch("app.services.memory_store.get_client", return_value=client):
            jobs = await claim_due_verifications()
        assert len(jobs) == 2
        assert {j["incident_id"] for j in jobs} == {"inc-1", "inc-2"}

    @pytest.mark.asyncio
    async def test_delete_verification(self):
        client = MagicMock()
        client.delete = AsyncMock(return_value={"result": "deleted"})
        with patch("app.services.memory_store.get_client", return_value=client):
            await delete_verification_job("inc-1")
        client.delete.assert_called_once()
        kwargs = client.delete.call_args.kwargs
        assert kwargs["id"] == "inc-1"


# ── Phase 6c — daily index + ILM ─────────────────────────────────────────────


class TestDailyIndexAndILM:
    def test_write_index_uses_today(self):
        idx = _write_index()
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        assert idx == f"devops-incidents-{today}"

    @pytest.mark.asyncio
    async def test_save_incident_uses_daily_index(self):
        client = MagicMock()
        client.index = AsyncMock(return_value={"_id": "inc-1"})
        with patch("app.services.memory_store.get_client", return_value=client):
            await save_incident({"incident_id": "inc-1", "service": "svc"})
        kwargs = client.index.call_args.kwargs
        assert kwargs["index"].startswith("devops-incidents-")
        # YYYY.MM.DD suffix
        assert len(kwargs["index"]) == len("devops-incidents-YYYY.MM.DD")
        assert kwargs["refresh"] == "wait_for"

    @pytest.mark.asyncio
    async def test_bootstrap_ilm_policy_idempotent(self):
        client = MagicMock()
        client.transport = MagicMock()
        client.transport.perform_request = AsyncMock(return_value={"acknowledged": True})
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await bootstrap_ilm_policy()
        assert ok is True
        # PUT to /_ilm/policy/<name>
        args = client.transport.perform_request.call_args
        assert args.args[0] == "PUT"
        assert "/_ilm/policy/" in args.args[1]

    @pytest.mark.asyncio
    async def test_bootstrap_ilm_policy_failure_does_not_raise(self):
        """Some ES distributions lack ILM — must not crash."""
        client = MagicMock()
        client.transport = MagicMock()
        client.transport.perform_request = AsyncMock(side_effect=Exception("not supported"))
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await bootstrap_ilm_policy()
        assert ok is False


# ── Phase 6h — execution lease ──────────────────────────────────────────────


class TestLease:
    @pytest.mark.asyncio
    async def test_acquire_lease_first_winner(self):
        client = MagicMock()
        client.index = AsyncMock(return_value={"_id": "lease-1"})
        with patch("app.services.memory_store.get_client", return_value=client):
            won = await try_acquire_lease("lease-1")
        assert won is True

    @pytest.mark.asyncio
    async def test_acquire_lease_conflict_returns_false(self):
        client = MagicMock()
        client.index = AsyncMock(side_effect=_es_conflict_error())
        with patch("app.services.memory_store.get_client", return_value=client):
            won = await try_acquire_lease("lease-1")
        assert won is False

    @pytest.mark.asyncio
    async def test_renew_lease_when_owner_matches(self):
        client = MagicMock()
        client.update = AsyncMock(return_value={"result": "updated"})
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await renew_lease("lease-1", owner=_hostname())
        assert ok is True

    @pytest.mark.asyncio
    async def test_renew_lease_returns_false_when_stolen(self):
        """Painless script returned 'noop' → lease was taken by another owner."""
        client = MagicMock()
        client.update = AsyncMock(return_value={"result": "noop"})
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await renew_lease("lease-1", owner="not-the-original-owner")
        assert ok is False

    @pytest.mark.asyncio
    async def test_renew_lease_missing_returns_false(self):
        client = MagicMock()
        client.update = AsyncMock(side_effect=Exception("404 not_found"))
        with patch("app.services.memory_store.get_client", return_value=client):
            ok = await renew_lease("lease-1")
        assert ok is False

    @pytest.mark.asyncio
    async def test_release_lease_only_when_owner_matches(self):
        client = MagicMock()
        client.delete_by_query = AsyncMock(return_value={"deleted": 1})
        with patch("app.services.memory_store.get_client", return_value=client):
            await release_lease("lease-1", owner="me")
        kwargs = client.delete_by_query.call_args.kwargs
        # The delete_by_query body must include the owner filter
        body = kwargs["body"]
        terms = body["query"]["bool"]["must"]
        assert any("owner" in t.get("term", {}) for t in terms)

    @pytest.mark.asyncio
    async def test_recover_orphans_returns_ids(self):
        client = MagicMock()
        client.search = AsyncMock(return_value={
            "hits": {"hits": [
                {"_id": "orphan-1", "_source": {"lease_id": "orphan-1"}},
                {"_id": "orphan-2", "_source": {"lease_id": "orphan-2"}},
            ]}
        })
        client.delete_by_query = AsyncMock(return_value={"deleted": 2})
        with patch("app.services.memory_store.get_client", return_value=client):
            ids = await recover_orphaned_leases(older_than_seconds=90)
        assert set(ids) == {"orphan-1", "orphan-2"}

    @pytest.mark.asyncio
    async def test_recover_orphans_no_orphans(self):
        client = MagicMock()
        client.search = AsyncMock(return_value={"hits": {"hits": []}})
        with patch("app.services.memory_store.get_client", return_value=client):
            ids = await recover_orphaned_leases(older_than_seconds=90)
        assert ids == []

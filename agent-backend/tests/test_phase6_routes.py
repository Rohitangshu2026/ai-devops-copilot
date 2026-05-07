"""Phase 6e/6f/6i — API route tests for new endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


# ── Phase 6f — incident status endpoint ──────────────────────────────────────


class TestIncidentStatus:
    def test_returns_404_when_missing(self, client):
        with patch("app.api.routes.get_incident", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            r = client.get("/api/v1/incidents/nonexistent")
        assert r.status_code == 404

    def test_returns_state_when_present(self, client):
        with patch("app.api.routes.get_incident", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "incident_id": "inc-1",
                "service": "svc",
                "action_state": "executing",
                "outcome": "unknown",
                "proposed_action": {"type": "restart_pod", "target": "svc"},
                "execution_result": None,
                "safety_decision": "allowed",
                "safety_reason": "ok",
            }
            r = client.get("/api/v1/incidents/inc-1")
        assert r.status_code == 200
        data = r.json()
        assert data["action_state"] == "executing"
        assert data["service"] == "svc"
        assert data["proposed_action"]["type"] == "restart_pod"

    def test_handles_completed_state(self, client):
        with patch("app.api.routes.get_incident", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "incident_id": "inc-2",
                "service": "svc",
                "action_state": "completed",
                "outcome": "resolved",
                "proposed_action": {"type": "restart_pod"},
                "execution_result": {"status": "success", "intended": 1, "achieved": 1},
                "safety_decision": "allowed",
                "safety_reason": "ok",
            }
            r = client.get("/api/v1/incidents/inc-2")
        assert r.status_code == 200
        data = r.json()
        assert data["action_state"] == "completed"
        assert data["outcome"] == "resolved"
        assert data["execution_result"]["status"] == "success"


# ── Phase 6e — unfreeze endpoint ─────────────────────────────────────────────


class TestUnfreezeEndpoint:
    def test_404_when_no_frozen_incident(self, client):
        # find_recent_actions returns nothing AND the fallback ES search also empty.
        with (
            patch("app.services.memory_store.find_recent_actions",
                  new_callable=AsyncMock) as mock_find,
        ):
            mock_find.return_value = []
            # Also patch the get_client used in the fallback path.
            es_client = MagicMock()
            es_client.search = AsyncMock(return_value={"hits": {"hits": []}})
            with patch("app.services.elk_service.get_client", return_value=es_client):
                r = client.post("/api/v1/services/myservice/unfreeze")
        assert r.status_code == 404

    def test_clears_frozen_incident(self, client):
        es_client = MagicMock()
        es_client.search = AsyncMock(return_value={
            "hits": {"hits": [
                {"_id": "1", "_source": {"incident_id": "inc-1", "service": "myservice"}}
            ]}
        })
        with (
            patch("app.services.memory_store.find_recent_actions",
                  new_callable=AsyncMock) as mock_find,
            patch("app.services.elk_service.get_client", return_value=es_client),
            patch("app.api.routes.update_incident",
                  new_callable=AsyncMock) as mock_update,
        ):
            mock_find.return_value = []
            r = client.post("/api/v1/services/myservice/unfreeze")
        assert r.status_code == 200
        assert r.json()["count"] == 1
        assert "inc-1" in r.json()["cleared_incidents"]
        # State was flipped
        update_kwargs = mock_update.call_args.args
        assert update_kwargs[0] == "inc-1"
        assert update_kwargs[1]["action_state"] == "unfrozen"


# ── Phase 6i — manual policy reload ──────────────────────────────────────────


class TestReloadPolicyEndpoint:
    def test_reload_returns_summary(self, client):
        from app.core.policy import _DEFAULTS, _build_from_dict
        with patch("app.api.routes.reload_policy", return_value=_build_from_dict(_DEFAULTS)):
            r = client.post("/api/v1/admin/reload-policy")
        assert r.status_code == 200
        data = r.json()
        assert "actions" in data
        assert "decision_rows" in data
        assert "global" in data
        # Defaults should include the standard actions.
        for a in ("restart_pod", "rollback", "notify", "no_action"):
            assert a in data["actions"]

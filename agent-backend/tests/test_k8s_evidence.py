"""Tests for app.services.k8s_evidence — Phase 2 (k8s events as evidence).

Design notes:
* All normalize_event tests are synchronous — k8s_evidence has no async surface.
* read_pod_status is patched via unittest.mock.patch so no live k8s cluster is needed.
* pod_status_cache is injected explicitly so tests are fully isolated from
  each other and from the module-level _DEFAULT_POD_STATUS_CACHE.
* The fetch_logs test stubs get_client to capture the ES query body without
  making a network call.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.k8s_evidence as ke
from app.services.elk_service import fetch_logs


# ── Helper factory ────────────────────────────────────────────────────────────


def _wevt(
    *,
    reason="OOMKilled",
    severity="critical",
    involved_kind="Pod",
    involved_name="auth-service-abc-xyz",
    service="auth-service",
    namespace="spyroom",
    count=4,
    message="Memory cgroup out of memory: Kill process 1234",
    first_seen="2024-01-01T00:00:00+00:00",
    last_seen="2024-01-01T00:01:00+00:00",
):
    """Build a WatcherEvent-like SimpleNamespace for normalize_event tests."""
    return SimpleNamespace(
        reason=reason,
        severity=severity,
        involved_kind=involved_kind,
        involved_name=involved_name,
        service=service,
        namespace=namespace,
        count=count,
        message=message,
        first_seen=first_seen,
        last_seen=last_seen,
    )


# ── Test 1: Pod kind — all keys present, pod_name matches involved_name ───────


def test_normalize_event_pod_kind_all_keys_present():
    with patch.object(ke, "read_pod_status", return_value={"restart_count": 3}):
        result = ke.normalize_event(_wevt(), pod_status_cache={})

    expected_keys = {
        "type", "severity", "reason", "message",
        "involved_kind", "involved_name", "service", "namespace",
        "pod_name", "restart_count", "restart_count_delta",
        "first_seen", "last_seen", "count",
        "level", "@timestamp", "endpoint",
    }
    assert set(result.keys()) == expected_keys
    assert result["pod_name"] == "auth-service-abc-xyz"
    assert result["type"] == "k8s_event"
    assert result["endpoint"] == ""
    assert result["count"] == 4
    assert result["first_seen"] == "2024-01-01T00:00:00+00:00"
    assert result["last_seen"] == "2024-01-01T00:01:00+00:00"
    assert result["@timestamp"] == "2024-01-01T00:01:00+00:00"


# ── Test 2: Deployment kind — pod_name is None, restart_count is None ─────────


def test_normalize_event_deployment_kind_pod_name_is_none():
    with patch.object(ke, "read_pod_status", return_value={}) as mock_read:
        result = ke.normalize_event(
            _wevt(involved_kind="Deployment", involved_name="auth-service"),
            pod_status_cache={},
        )
    # read_pod_status should NOT be called for non-Pod events
    mock_read.assert_not_called()
    assert result["pod_name"] is None
    assert result["restart_count"] is None
    assert result["restart_count_delta"] is None
    # All other keys still present
    assert result["type"] == "k8s_event"
    assert result["involved_kind"] == "Deployment"
    assert result["involved_name"] == "auth-service"


# ── Test 3: Critical severity → ERROR level ───────────────────────────────────


def test_normalize_event_critical_severity_maps_to_error_level():
    with patch.object(ke, "read_pod_status", return_value={"restart_count": 1}):
        result = ke.normalize_event(
            _wevt(reason="OOMKilled", severity="critical"),
            pod_status_cache={},
        )
    assert result["severity"] == "critical"
    assert result["level"] == "ERROR"


# ── Test 4: Warning severity → WARNING level ──────────────────────────────────


def test_normalize_event_warning_severity_maps_to_warning_level():
    with patch.object(ke, "read_pod_status", return_value={"restart_count": 1}):
        result = ke.normalize_event(
            _wevt(reason="Unhealthy", severity="warning"),
            pod_status_cache={},
        )
    assert result["severity"] == "warning"
    assert result["level"] == "WARNING"


# ── Test 5: Restart delta — first sight is 0, count stored in cache ───────────


def test_normalize_event_restart_delta_first_sight_is_zero():
    cache: dict = {}
    with patch.object(ke, "read_pod_status", return_value={"restart_count": 5}):
        result = ke.normalize_event(_wevt(), pod_status_cache=cache)

    assert result["restart_count"] == 5
    assert result["restart_count_delta"] == 0
    assert cache[("spyroom", "auth-service-abc-xyz")] == 5


# ── Test 6: Restart delta — subsequent sight reflects new restarts ─────────────


def test_normalize_event_restart_delta_subsequent_sight():
    cache: dict = {("spyroom", "auth-service-abc-xyz"): 3}
    with patch.object(ke, "read_pod_status", return_value={"restart_count": 7}):
        result = ke.normalize_event(_wevt(), pod_status_cache=cache)

    assert result["restart_count"] == 7
    assert result["restart_count_delta"] == 4
    assert cache[("spyroom", "auth-service-abc-xyz")] == 7


# ── Test 7: Restart delta after cache eviction — treated as first sight ────────


def test_normalize_event_restart_delta_after_eviction_treated_as_first_sight():
    # Fill cache to _POD_CACHE_MAX with unrelated entries so the pod under test
    # is absent (simulating post-eviction state)
    cache: dict = {(f"ns-{i}", f"pod-{i}"): i for i in range(ke._POD_CACHE_MAX)}
    assert len(cache) == ke._POD_CACHE_MAX

    pod_key = ("spyroom", "auth-service-abc-xyz")
    assert pod_key not in cache

    with patch.object(ke, "read_pod_status", return_value={"restart_count": 10}):
        result = ke.normalize_event(_wevt(), pod_status_cache=cache)

    # First sight → delta == 0
    assert result["restart_count_delta"] == 0
    assert result["restart_count"] == 10
    # Eviction happened: cache size stays at _POD_CACHE_MAX, pod_key is now in it
    assert len(cache) == ke._POD_CACHE_MAX
    assert pod_key in cache


# ── Test 8: fetch_logs namespace + pod_name add correct ES term clauses ────────


@pytest.mark.asyncio
async def test_fetch_logs_namespace_and_pod_name_add_term_clauses():
    # Capture only the FIRST search call — fetch_logs fires a second call
    # via _log_stale_data_hint (no-docs path) which uses a different query.
    calls: list = []

    async def _fake_search(index, body):
        calls.append(body)
        return {"hits": {"hits": []}}

    with patch("app.services.elk_service.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.search.side_effect = _fake_search
        mock_get_client.return_value = mock_client

        await fetch_logs(
            "auth-service",
            "dev",
            15,
            namespace="spyroom",
            pod_name="auth-service-abc-xyz",
        )

    # The first call is the main query with all must clauses
    must = calls[0]["query"]["bool"]["must"]
    ns_clause = {"term": {"kubernetes.namespace.keyword": "spyroom"}}
    pod_clause = {"term": {"kubernetes.pod.name.keyword": "auth-service-abc-xyz"}}
    assert ns_clause in must, f"namespace term clause missing from: {must}"
    assert pod_clause in must, f"pod_name term clause missing from: {must}"


# ── Test 9: fetch_logs without namespace/pod_name — clauses absent ────────────


@pytest.mark.asyncio
async def test_fetch_logs_without_namespace_pod_name_no_extra_clauses():
    calls: list = []

    async def _fake_search(index, body):
        calls.append(body)
        return {"hits": {"hits": []}}

    with patch("app.services.elk_service.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.search.side_effect = _fake_search
        mock_get_client.return_value = mock_client

        await fetch_logs("auth-service", "dev", 15)

    must_str = str(calls[0]["query"]["bool"]["must"])
    assert "kubernetes.namespace.keyword" not in must_str
    assert "kubernetes.pod.name.keyword" not in must_str

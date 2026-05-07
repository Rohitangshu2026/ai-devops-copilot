"""Phase 7e — unit tests for the get_k8s_events agentic tool.

All tests run in CI with no real Kubernetes cluster and no real Elasticsearch.
Graceful degradation is the primary invariant: every failure mode must return
"k8s events unavailable" rather than raising an exception into the agentic loop.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_k8s_event(
    obj_name: str,
    event_type: str,
    reason: str,
    message: str,
    minutes_ago: float,
) -> MagicMock:
    """Build a MagicMock that looks like a kubernetes.client.V1Event."""
    event = MagicMock()
    event.last_timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    event.event_time = None
    event.type = event_type
    event.reason = reason
    event.message = message
    event.involved_object = MagicMock()
    event.involved_object.name = obj_name
    return event


def _make_old_event(obj_name: str) -> MagicMock:
    """Return an event with a timestamp older than any reasonable lookback window."""
    return _make_k8s_event(obj_name, "Warning", "BackOff", "Old event", minutes_ago=999)


def _k8s_resp(events: list) -> MagicMock:
    resp = MagicMock()
    resp.items = events
    return resp


# ── Schema tests ──────────────────────────────────────────────────────────────


class TestToolSchema:
    def test_tools_list_has_three_entries(self):
        from app.llm.tools import TOOLS
        assert len(TOOLS) == 3

    def test_k8s_events_tool_schema_correct(self):
        from app.llm.tools import TOOLS
        k8s_tool = next(t for t in TOOLS if t["name"] == "get_k8s_events")
        assert "namespace" in k8s_tool["input_schema"]["properties"]
        assert "lookback_minutes" in k8s_tool["input_schema"]["properties"]
        desc = k8s_tool["description"].lower()
        assert "k8s" in desc or "kubernetes" in desc

    def test_openai_tools_includes_k8s_events(self):
        from app.llm.tools import OPENAI_TOOLS
        names = [t["function"]["name"] for t in OPENAI_TOOLS]
        assert "get_k8s_events" in names
        assert len(OPENAI_TOOLS) == 3


# ── Graceful degradation tests ────────────────────────────────────────────────
# We patch at the installed kubernetes module level since the package is present.


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_import_error_returns_unavailable(self):
        """If the kubernetes package is not importable, return unavailable sentinel."""
        from app.llm.tools import _get_k8s_events

        # Temporarily remove kubernetes from sys.modules so the import fails
        saved = {k: v for k, v in sys.modules.items() if "kubernetes" in k}
        for k in list(saved):
            sys.modules.pop(k, None)

        try:
            # Also block the import at builtins level
            original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

            def _block_kubernetes(name, *args, **kwargs):
                if name == "kubernetes" or name.startswith("kubernetes."):
                    raise ImportError(f"No module named '{name}'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_block_kubernetes):
                result = await _get_k8s_events("default", 15, "sample-app")

        finally:
            # Restore kubernetes modules
            sys.modules.update(saved)

        assert result == "k8s events unavailable"

    @pytest.mark.asyncio
    async def test_no_kubeconfig_returns_unavailable(self):
        """When both in-cluster and local kubeconfig loading fail, return unavailable."""
        from app.llm.tools import _get_k8s_events

        with (
            patch("kubernetes.config.load_incluster_config",
                  side_effect=Exception("not in cluster")),
            patch("kubernetes.config.load_kube_config",
                  side_effect=FileNotFoundError("~/.kube/config not found")),
        ):
            result = await _get_k8s_events("default", 15, "sample-app")

        assert result == "k8s events unavailable"

    @pytest.mark.asyncio
    async def test_api_exception_returns_unavailable(self):
        """Any exception from list_namespaced_event → unavailable sentinel."""
        from app.llm.tools import _get_k8s_events

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_event.side_effect = Exception("ApiException: 403 Forbidden")

        with (
            patch("kubernetes.config.load_incluster_config", return_value=None),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
        ):
            result = await _get_k8s_events("default", 15, "sample-app")

        assert result == "k8s events unavailable"

    @pytest.mark.asyncio
    async def test_runtime_error_returns_unavailable(self):
        """An unexpected RuntimeError anywhere in the function → unavailable."""
        from app.llm.tools import _get_k8s_events

        with (
            patch("kubernetes.config.load_incluster_config",
                  side_effect=RuntimeError("unexpected network error")),
            patch("kubernetes.config.load_kube_config",
                  side_effect=RuntimeError("unexpected network error")),
        ):
            result = await _get_k8s_events("default", 15, "sample-app")

        assert result == "k8s events unavailable"

    @pytest.mark.asyncio
    async def test_empty_event_list_returns_no_events_message(self):
        """When the cluster returns no events → human-readable 'not found' message."""
        from app.llm.tools import _get_k8s_events

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_event.return_value = _k8s_resp([])

        with (
            patch("kubernetes.config.load_incluster_config", return_value=None),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
        ):
            result = await _get_k8s_events("default", 15, "sample-app")

        assert "No k8s events found" in result
        assert "sample-app" in result


# ── Happy path tests ──────────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_single_event_formatted_correctly(self):
        """A single in-window event is formatted as [ts] type/reason: message (object: name)."""
        from app.llm.tools import _get_k8s_events

        event = _make_k8s_event(
            "sample-app-abc123",
            "Warning",
            "BackOff",
            "Back-off restarting failed container",
            minutes_ago=2,
        )
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_event.return_value = _k8s_resp([event])

        with (
            patch("kubernetes.config.load_incluster_config", return_value=None),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
        ):
            result = await _get_k8s_events("default", 15, "sample-app")

        assert "Warning/BackOff" in result
        assert "Back-off restarting" in result
        assert "sample-app-abc123" in result

    @pytest.mark.asyncio
    async def test_old_events_filtered_out(self):
        """Events older than lookback_minutes are excluded from results."""
        from app.llm.tools import _get_k8s_events

        recent = _make_k8s_event("svc-pod", "Warning", "OOMKilling", "OOM", minutes_ago=3)
        old = _make_old_event("svc-pod")

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_event.return_value = _k8s_resp([recent, old])

        with (
            patch("kubernetes.config.load_incluster_config", return_value=None),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
        ):
            result = await _get_k8s_events("default", 15, "svc")

        lines = result.strip().split("\n")
        assert len(lines) == 1
        assert "OOMKilling" in lines[0]

    @pytest.mark.asyncio
    async def test_results_capped_at_20_events(self):
        """More than 20 in-window events are capped to 20 lines."""
        from app.llm.tools import _get_k8s_events

        events = [
            _make_k8s_event("pod", "Warning", "BackOff", f"msg {i}", minutes_ago=i * 0.1)
            for i in range(25)
        ]
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_event.return_value = _k8s_resp(events)

        with (
            patch("kubernetes.config.load_incluster_config", return_value=None),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
        ):
            result = await _get_k8s_events("default", 60, "svc")

        lines = result.strip().split("\n")
        assert len(lines) == 20


# ── Dispatcher integration tests ──────────────────────────────────────────────


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_execute_tool_dispatches_get_k8s_events(self):
        """execute_tool routes get_k8s_events calls to _get_k8s_events."""
        from app.llm.tools import execute_tool

        with patch("app.llm.tools._get_k8s_events", new_callable=AsyncMock) as mock_fn:
            mock_fn.return_value = "k8s events unavailable"
            result = await execute_tool(
                "get_k8s_events",
                {"namespace": "production"},
                "my-service",
                30,
            )

        mock_fn.assert_awaited_once_with(
            namespace="production",
            lookback_minutes=30,
            service="my-service",
        )
        assert result == "k8s events unavailable"

    @pytest.mark.asyncio
    async def test_execute_tool_defaults_namespace_to_default(self):
        """When namespace is absent from tool_input, 'default' is used."""
        from app.llm.tools import execute_tool

        with patch("app.llm.tools._get_k8s_events", new_callable=AsyncMock) as mock_fn:
            mock_fn.return_value = "k8s events unavailable"
            await execute_tool("get_k8s_events", {}, "svc", 15)

        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["namespace"] == "default"

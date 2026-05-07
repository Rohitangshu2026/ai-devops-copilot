"""Phase 7c — chaos and concurrency e2e tests.

These tests are marked @cluster_ready and @pytest.mark.e2e.
They verify the safety stack handles adversarial conditions gracefully:
  - 10 concurrent analyze calls → no duplicate incident_ids, no crashes
  - agent-backend survives while the sample-app pod is killed mid-request
  - ES being unavailable produces a human-readable error (not a stack trace)
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import pytest

from tests.e2e.conftest import cluster_ready, NAMESPACE, AGENT_URL

pytestmark = [cluster_ready, pytest.mark.e2e]


def _kubectl(*args: str, timeout: int = 60) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def _analyze(payload: dict | None = None) -> tuple[int, dict]:
    """Send a single /analyze request and return (status_code, body)."""
    body = payload or {"service": "sample-app", "environment": "dev", "lookback_minutes": 5}
    try:
        r = httpx.post(f"{AGENT_URL}/api/v1/analyze", json=body, timeout=60)
        return r.status_code, r.json() if r.status_code in (200, 422, 500) else {}
    except Exception as exc:
        return 0, {"error": str(exc)}


class TestConcurrentAnalyze:
    def test_10_concurrent_analyze_no_duplicate_incident_ids(self):
        """10 parallel /analyze calls → all return 200 or 500, no duplicate incident_ids."""
        # First trigger some errors to ensure logs exist
        for _ in range(5):
            try:
                httpx.get(f"{AGENT_URL.replace('8001', '8000')}/error", timeout=5)
            except Exception:
                pass
        time.sleep(12)

        n = 10
        results: list[tuple[int, dict]] = []
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_analyze) for _ in range(n)]
            for f in as_completed(futures):
                results.append(f.result())

        statuses = [r[0] for r in results]
        # All calls must return a valid HTTP status (not 0 = network failure)
        assert all(s in (200, 500) for s in statuses), (
            f"Unexpected status codes: {statuses}"
        )

        # No panic / unhandled exceptions (we allow 500 for "no logs" edge case)
        incident_ids = [
            r[1].get("incident_id")
            for status, r in results
            if status == 200 and r.get("incident_id")
        ]
        # All incident_ids must be unique (no duplicate execution)
        assert len(incident_ids) == len(set(incident_ids)), (
            f"Duplicate incident_ids found: {incident_ids}"
        )

    def test_agent_backend_still_healthy_after_concurrent_load(self):
        """After concurrent requests the agent-backend /health endpoint still returns 200."""
        n = 5
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_analyze) for _ in range(n)]
            for f in as_completed(futures):
                f.result()  # consume results to avoid thread leak

        r = httpx.get(f"{AGENT_URL}/health", timeout=10)
        assert r.status_code == 200, (
            f"agent-backend unhealthy after concurrent load: {r.text}"
        )


class TestPodCrashResilience:
    def test_agent_backend_survives_sample_app_pod_deletion(self):
        """Killing the sample-app pod mid-analysis does not crash agent-backend."""
        # Trigger some log data
        for _ in range(3):
            try:
                httpx.get(f"{AGENT_URL.replace('8001', '8000')}/error", timeout=5)
            except Exception:
                pass
        time.sleep(8)

        # Start an analysis in a background thread
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_analyze)

            # Simultaneously delete the sample-app pod
            time.sleep(0.5)
            try:
                _kubectl("delete", "pod", "-l", "app=sample-app", "-n", NAMESPACE, "--timeout=10s")
            except Exception:
                pass  # pod may already be in termination

            status, body = future.result()

        # The analysis may fail (500 / "no logs") but must not cause a panic.
        assert status in (200, 500), f"Unexpected status: {status}\n{body}"

        # agent-backend must still be healthy
        r = httpx.get(f"{AGENT_URL}/health", timeout=15)
        assert r.status_code == 200, f"agent-backend unhealthy after pod kill: {r.text}"

        # Wait for sample-app to recover (k8s restarts it automatically)
        _kubectl("rollout", "status", "deployment/sample-app", "-n", NAMESPACE, "--timeout=60s")


class TestElasticsearchUnavailable:
    def test_es_unavailable_returns_human_readable_error(self):
        """When ES is scaled to 0, /analyze returns 500 with a readable message, not a stack trace."""
        try:
            _kubectl("scale", "deployment/elasticsearch", "--replicas=0", "-n", NAMESPACE)
            time.sleep(10)  # wait for ES to become unreachable

            status, body = _analyze()

            assert status == 500, f"Expected 500 when ES is down, got {status}"
            # Should be a structured error, not a raw Python traceback
            if isinstance(body, dict):
                detail = str(body.get("detail", body.get("error", ""))).lower()
                assert "traceback" not in detail, (
                    f"Raw traceback leaked in error response: {detail[:200]}"
                )

        finally:
            _kubectl("scale", "deployment/elasticsearch", "--replicas=1", "-n", NAMESPACE)
            # Wait for ES to become ready before other tests run
            _kubectl(
                "rollout", "status", "deployment/elasticsearch",
                "-n", NAMESPACE, "--timeout=300s",
                timeout=360,
            )

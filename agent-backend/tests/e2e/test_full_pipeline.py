"""Phase 7b — end-to-end pipeline test scenarios.

All tests are marked @cluster_ready and @pytest.mark.e2e — they are skipped
unless a kind cluster is running and the agent-backend is port-forwarded.

Run after 'bash scripts/e2e_setup.sh':
    kubectl port-forward svc/agent-backend  8001:8001 -n devops-test &
    kubectl port-forward svc/sample-app     8000:8000 -n devops-test &
    pytest tests/e2e/test_full_pipeline.py -v -s
"""
from __future__ import annotations

import subprocess
import time

import pytest

from tests.e2e.conftest import cluster_ready, NAMESPACE

pytestmark = [cluster_ready, pytest.mark.e2e]

_KNOWN_ACTIONS = {"restart_pod", "rollback", "scale_up", "trigger_retry", "notify", "no_action"}


def _kubectl(*args: str) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def _set_env(deployment: str, **envs: str) -> None:
    """Set one or more env vars on a deployment and wait for rollout."""
    pairs = [f"{k}={v}" for k, v in envs.items()]
    _kubectl("set", "env", f"deployment/{deployment}", *pairs, "-n", NAMESPACE)
    _kubectl("rollout", "status", f"deployment/{deployment}", "-n", NAMESPACE, "--timeout=60s")


def _reset_env(deployment: str, *keys: str) -> None:
    """Remove env vars (reset to default) and wait for rollout."""
    pairs = [f"{k}-" for k in keys]
    _kubectl("set", "env", f"deployment/{deployment}", *pairs, "-n", NAMESPACE)
    _kubectl("rollout", "status", f"deployment/{deployment}", "-n", NAMESPACE, "--timeout=60s")


# ── Scenario 1 & 2: Health checks + error burst ───────────────────────────────


class TestHealthAndBasicAnalysis:
    def test_e2e_health_endpoints(self, agent_client, sample_app_client):
        """Both agent-backend and sample-app /health endpoints return 200."""
        r1 = agent_client.get("/health")
        assert r1.status_code == 200, f"agent-backend /health: {r1.text}"

        r2 = sample_app_client.get("/health")
        assert r2.status_code == 200, f"sample-app /health: {r2.text}"

    def test_e2e_error_burst_then_analyze(self, sample_app_client, agent_client):
        """Trigger 10 errors, wait for ES ingestion, run analysis → incident_id present."""
        # Trigger errors
        for _ in range(10):
            sample_app_client.get("/error")

        # Allow logs to reach ES (Filebeat + Logstash pipeline)
        time.sleep(15)

        r = agent_client.post(
            "/api/v1/analyze",
            json={"service": "sample-app", "environment": "dev", "lookback_minutes": 5},
        )
        assert r.status_code == 200, f"Unexpected status: {r.status_code}\n{r.text}"
        data = r.json()

        assert data.get("root_cause"), "root_cause must be non-empty"
        assert data.get("incident_id"), "incident_id must be present"
        assert data.get("safety_decision") in ("allowed", "denied")

    def test_e2e_analysis_result_schema_valid(self, sample_app_client, agent_client):
        """Analysis response body conforms to the AnalysisResult Pydantic schema."""
        for _ in range(5):
            sample_app_client.get("/error")
        time.sleep(12)

        r = agent_client.post(
            "/api/v1/analyze",
            json={"service": "sample-app", "environment": "dev", "lookback_minutes": 5},
        )
        assert r.status_code == 200
        data = r.json()

        # Validate required fields
        assert "service" in data
        assert "root_cause" in data
        assert "confidence_hint" in data
        assert data["confidence_hint"] in ("low", "medium", "high")
        assert "proposed_action" in data
        assert data["proposed_action"]["type"] in _KNOWN_ACTIONS
        assert "safety_decision" in data
        assert "confidence_breakdown" in data
        assert isinstance(data["confidence_breakdown"], list)


# ── Scenario 4: /slow endpoint ────────────────────────────────────────────────


class TestSlowEndpointScenario:
    def test_e2e_slow_endpoint_triggers_analysis(self, sample_app_client, agent_client):
        """Set SLOW_MS=3000 (> SLOW_THRESHOLD_MS=2000), hit /slow, run analysis."""
        try:
            _set_env("sample-app", SLOW_MS="3000", SLOW_THRESHOLD_MS="2000")

            # Generate slow/timeout log entries
            for _ in range(5):
                try:
                    sample_app_client.get("/slow", timeout=10)
                except Exception:
                    pass  # 504 is expected

            time.sleep(12)

            r = agent_client.post(
                "/api/v1/analyze",
                json={"service": "sample-app", "environment": "dev", "lookback_minutes": 3},
            )
            assert r.status_code == 200
            data = r.json()
            assert data.get("root_cause"), "root_cause must be non-empty after slow-endpoint burst"

        finally:
            _reset_env("sample-app", "SLOW_MS", "SLOW_THRESHOLD_MS")


# ── Scenario 5: /crash endpoint ───────────────────────────────────────────────


class TestCrashEndpointScenario:
    def test_e2e_crash_endpoint_triggers_high_severity(self, sample_app_client, agent_client):
        """Set CRASH_MESSAGE, hit /crash 5 times, analysis detects high/critical severity."""
        try:
            _set_env("sample-app", CRASH_MESSAGE="k8s e2e test crash injection")

            for _ in range(5):
                try:
                    sample_app_client.get("/crash", timeout=5)
                except Exception:
                    pass  # 500 is expected

            time.sleep(12)

            r = agent_client.post(
                "/api/v1/analyze",
                json={"service": "sample-app", "environment": "dev", "lookback_minutes": 3},
            )
            assert r.status_code == 200
            data = r.json()
            severity = data.get("parsed_log", {}).get("severity")
            assert severity in ("high", "critical"), (
                f"Expected high/critical severity for crash logs, got: {severity}"
            )

        finally:
            _reset_env("sample-app", "CRASH_MESSAGE")


# ── Scenario 6: /dep-error endpoint ──────────────────────────────────────────


class TestDepErrorScenario:
    def test_e2e_dep_error_triggers_dependency_analysis(self, sample_app_client, agent_client):
        """Scale mock-downstream to 0, set DOWNSTREAM_URL, hit /dep-error → dependency_error."""
        try:
            # Kill the mock downstream
            _kubectl("scale", "deployment/mock-downstream", "--replicas=0", "-n", NAMESPACE)
            time.sleep(5)

            # Point sample-app at the (now-dead) downstream
            _set_env(
                "sample-app",
                DOWNSTREAM_URL="http://mock-downstream:8000",
            )

            for _ in range(5):
                try:
                    sample_app_client.get("/dep-error", timeout=8)
                except Exception:
                    pass  # 503 is expected

            time.sleep(12)

            r = agent_client.post(
                "/api/v1/analyze",
                json={"service": "sample-app", "environment": "dev", "lookback_minutes": 3},
            )
            assert r.status_code == 200
            data = r.json()
            error_type = data.get("parsed_log", {}).get("error_type")
            assert error_type == "dependency_error", (
                f"Expected dependency_error, got: {error_type}"
            )

        finally:
            _kubectl("scale", "deployment/mock-downstream", "--replicas=1", "-n", NAMESPACE)
            _reset_env("sample-app", "DOWNSTREAM_URL")

    def test_e2e_dep_error_causality_verified(self, sample_app_client, agent_client):
        """Connection-refused dep-error logs → causality_verified=True."""
        try:
            _kubectl("scale", "deployment/mock-downstream", "--replicas=0", "-n", NAMESPACE)
            time.sleep(5)
            _set_env("sample-app", DOWNSTREAM_URL="http://mock-downstream:8000")

            for _ in range(5):
                try:
                    sample_app_client.get("/dep-error", timeout=8)
                except Exception:
                    pass

            time.sleep(12)

            r = agent_client.post(
                "/api/v1/analyze",
                json={"service": "sample-app", "environment": "dev", "lookback_minutes": 3},
            )
            assert r.status_code == 200
            data = r.json()
            assert data.get("causality_verified") is True

        finally:
            _kubectl("scale", "deployment/mock-downstream", "--replicas=1", "-n", NAMESPACE)
            _reset_env("sample-app", "DOWNSTREAM_URL")

"""E2E test fixtures and skip markers.

Tests in this directory are skipped automatically unless:
  1. The `kind` binary is installed.
  2. A kind cluster named 'devops-copilot-e2e' (or E2E_CLUSTER_NAME) is running.
  3. The agent-backend is reachable at E2E_AGENT_URL (default: http://localhost:8001).

Run scripts/e2e_setup.sh + port-forwarding before executing these tests.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import httpx
import pytest

NAMESPACE = "devops-test"
AGENT_URL = os.getenv("E2E_AGENT_URL", "http://localhost:8001")
SAMPLE_APP_URL = os.getenv("E2E_SAMPLE_APP_URL", "http://localhost:8000")
CLUSTER_NAME = os.getenv("E2E_CLUSTER_NAME", "devops-copilot-e2e")


def _kind_available() -> bool:
    return shutil.which("kind") is not None


def _cluster_running() -> bool:
    if not _kind_available():
        return False
    try:
        result = subprocess.run(
            ["kind", "get", "clusters"],
            capture_output=True, text=True, timeout=10,
        )
        return CLUSTER_NAME in result.stdout.splitlines()
    except Exception:
        return False


def _agent_reachable() -> bool:
    try:
        r = httpx.get(f"{AGENT_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


_CLUSTER_READY = _kind_available() and _cluster_running() and _agent_reachable()

cluster_ready = pytest.mark.skipif(
    not _CLUSTER_READY,
    reason=(
        "E2E cluster not available. Run 'bash scripts/e2e_setup.sh' and "
        "port-forward before running e2e tests."
    ),
)


@pytest.fixture(scope="session")
def agent_client():
    """Synchronous HTTP client pointing at the agent-backend."""
    return httpx.Client(base_url=AGENT_URL, timeout=90)


@pytest.fixture(scope="session")
def sample_app_client():
    """Synchronous HTTP client pointing at the sample-app."""
    return httpx.Client(base_url=SAMPLE_APP_URL, timeout=30)

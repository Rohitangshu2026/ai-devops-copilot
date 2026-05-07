"""Sample-app tests — covers original endpoints and Phase 7 failure modes."""
from fastapi.testclient import TestClient
from app import app

client = TestClient(app, raise_server_exceptions=False)


# ── Original endpoints ────────────────────────────────────────────────────────


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_error_returns_error_status():
    response = client.get("/error")
    assert response.status_code == 200
    assert response.json()["status"] == "error"


# ── /slow ─────────────────────────────────────────────────────────────────────


def test_slow_default_no_delay():
    response = client.get("/slow")
    assert response.status_code == 200
    assert response.json()["delay_ms"] == 0


def test_slow_under_threshold_returns_200(monkeypatch):
    monkeypatch.setenv("SLOW_MS", "100")
    monkeypatch.setenv("SLOW_THRESHOLD_MS", "2000")
    response = client.get("/slow")
    assert response.status_code == 200
    assert response.json()["delay_ms"] == 100


def test_slow_over_threshold_returns_504(monkeypatch):
    monkeypatch.setenv("SLOW_MS", "3000")
    monkeypatch.setenv("SLOW_THRESHOLD_MS", "2000")
    response = client.get("/slow")
    assert response.status_code == 504
    assert response.json()["status"] == "timeout"


# ── /oom ──────────────────────────────────────────────────────────────────────


def test_oom_disabled_by_default():
    response = client.get("/oom")
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def test_oom_allocates_when_env_set(monkeypatch):
    monkeypatch.setenv("MEM_MB", "1")  # 1 MB — safe for test environment
    response = client.get("/oom")
    assert response.status_code == 200
    assert response.json()["status"] == "allocated"
    assert response.json()["mem_mb"] == 1


# ── /crash ────────────────────────────────────────────────────────────────────


def test_crash_returns_500():
    """FastAPI converts an unhandled RuntimeError to a 500 Internal Server Error."""
    response = client.get("/crash")
    assert response.status_code == 500


def test_crash_uses_custom_env_message(monkeypatch):
    monkeypatch.setenv("CRASH_MESSAGE", "custom-test-crash-message")
    response = client.get("/crash")
    assert response.status_code == 500


# ── /dep-error ────────────────────────────────────────────────────────────────


def test_dep_error_disabled_by_default():
    response = client.get("/dep-error")
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def test_dep_error_returns_503_on_connection_failure(monkeypatch):
    """Port 1 is reserved and should refuse connections on any platform."""
    monkeypatch.setenv("DOWNSTREAM_URL", "http://localhost:1")
    response = client.get("/dep-error")
    assert response.status_code == 503
    assert response.json()["status"] == "error"

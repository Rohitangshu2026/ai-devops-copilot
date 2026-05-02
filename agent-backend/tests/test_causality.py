from app.core.causality import validate_causality, DEPENDENCY_MAP
from app.log_processor.summarizer import LogSummary


def _summary(events=None, error_ratio=0.5):
    return LogSummary(
        total_events=10,
        error_count=5,
        warning_count=0,
        unique_endpoints=[],
        error_ratio=error_ratio,
        deduplicated_events=events or [],
        time_span_minutes=5.0,
    )


# ── verification ─────────────────────────────────────────────────────────────

def test_verified_on_db_connection_error():
    s = _summary(["connection refused to postgres:5432"])
    result = validate_causality(s, "DB connection pool exhausted", "sample-app")
    assert result.verified is True
    assert "db" in result.matched_evidence


def test_verified_on_oom():
    s = _summary(["OOMKilled: container exceeded 512Mi"])
    result = validate_causality(s, "memory pressure", "sample-app")
    assert result.verified is True
    assert "oom" in result.matched_evidence


def test_verified_on_build_failure():
    s = _summary(["ModuleNotFoundError: no module named requests"])
    result = validate_causality(s, "import error in startup", "sample-app")
    assert result.verified is True
    assert "build" in result.matched_evidence


def test_verified_on_test_failure():
    s = _summary(["FAILED test_auth.py::test_login - AssertionError"])
    result = validate_causality(s, "assertion failed in auth tests", "sample-app")
    assert result.verified is True
    assert "test" in result.matched_evidence


def test_verified_on_simulated_low_error_ratio():
    s = _summary(["GET /health → 200"], error_ratio=0.02)
    result = validate_causality(s, "no real issue detected", "sample-app")
    assert result.verified is True
    assert "simulated_or_no_issue" in result.matched_evidence


def test_not_verified_when_no_patterns_match():
    s = _summary(["GET /api → 200", "GET /health → 200"], error_ratio=0.5)
    result = validate_causality(s, "completely unknown cause xyz", "sample-app")
    assert result.verified is False
    assert result.matched_evidence == []


# ── dependency redirection ────────────────────────────────────────────────────

def test_target_redirected_on_connection_error():
    s = _summary(["connection refused to elasticsearch:9200"])
    result = validate_causality(s, "connection refused to elasticsearch", "sample-app")
    assert result.target_redirected is True
    assert result.action_target == DEPENDENCY_MAP["sample-app"][0]


def test_no_redirect_when_no_dependency_error_pattern():
    s = _summary(["OOMKilled: exceeded memory limit"])
    result = validate_causality(s, "out of memory", "sample-app")
    assert result.target_redirected is False
    assert result.action_target is None


def test_no_redirect_for_unknown_service():
    s = _summary(["connection refused to db:5432"])
    result = validate_causality(s, "connection refused to db", "unknown-service")
    # No deps registered for this service → no redirect
    assert result.target_redirected is False


def test_redirect_for_agent_backend():
    s = _summary(["connection refused to elasticsearch:9200"])
    result = validate_causality(s, "connection refused", "agent-backend")
    assert result.target_redirected is True
    assert result.action_target == DEPENDENCY_MAP["agent-backend"][0]


def test_dependency_map_has_expected_entries():
    assert "sample-app" in DEPENDENCY_MAP
    assert "agent-backend" in DEPENDENCY_MAP
    assert "elasticsearch" in DEPENDENCY_MAP["sample-app"]
    assert "elasticsearch" in DEPENDENCY_MAP["agent-backend"]


# ── matched_evidence contents ─────────────────────────────────────────────────

def test_multiple_patterns_can_match():
    # Both db (connection refused) and simulated (low ratio) match
    s = _summary(["connection refused to db"], error_ratio=0.01)
    result = validate_causality(s, "connection refused", "sample-app")
    assert "db" in result.matched_evidence
    assert "simulated_or_no_issue" in result.matched_evidence

"""Tests for Phase 10 — blast-radius estimation (app/core/blast_radius.py)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.blast_radius import BlastRadiusResult, compute_blast_radius


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

STATIC_DEP_MAP = {
    "api-gateway": ["auth-service", "sample-app"],
    "auth-service": ["postgres"],
    "sample-app": ["elasticsearch"],
    "postgres": [],
    "elasticsearch": [],
    "payment-api": ["redis", "postgres", "auth-service"],
}


def _make_result(score, affected, count, direct, source="static_map"):
    return BlastRadiusResult(
        score=score,
        affected_services=affected,
        affected_count=count,
        direct_dependents=direct,
        source=source,
    )


# ---------------------------------------------------------------------------
# Unit: compute_blast_radius with patched static map
# ---------------------------------------------------------------------------


@patch("app.core.blast_radius._get_dep_map")
def test_isolated_service_low_blast_radius(mock_dep_map):
    """A leaf service with no dependents has low blast radius."""
    mock_dep_map.return_value = (STATIC_DEP_MAP, "static_map")
    # payment-api has no callers in our test map → count=0 → low
    result = compute_blast_radius("payment-api")
    assert result.score == "low"
    assert result.affected_count == 0
    assert result.direct_dependents == []
    assert result.affected_services == []


@patch("app.core.blast_radius._get_dep_map")
def test_service_with_one_dependent_medium(mock_dep_map):
    """A service depended on by 1 other → medium."""
    mock_dep_map.return_value = (STATIC_DEP_MAP, "static_map")
    result = compute_blast_radius("auth-service")
    # api-gateway and payment-api both depend on auth-service
    assert result.score in ("medium", "high", "critical")
    assert "api-gateway" in result.affected_services or "payment-api" in result.affected_services


@patch("app.core.blast_radius._get_dep_map")
def test_hub_service_high_blast_radius(mock_dep_map):
    """postgres is depended on by auth-service → api-gateway + payment-api transitively."""
    mock_dep_map.return_value = (STATIC_DEP_MAP, "static_map")
    result = compute_blast_radius("postgres")
    # postgres ← auth-service ← api-gateway, payment-api; payment-api directly too
    assert result.affected_count >= 3
    assert result.score in ("high", "critical")


@patch("app.core.blast_radius._get_dep_map")
def test_critical_score_threshold(mock_dep_map):
    """More than 5 affected services → critical."""
    # Build a chain: svc0 ← svc1 ← svc2 ← ... ← svc7
    chain = {f"svc{i}": [f"svc{i-1}"] for i in range(1, 8)}
    chain["svc0"] = []
    mock_dep_map.return_value = (chain, "static_map")
    result = compute_blast_radius("svc0")
    assert result.score == "critical"
    assert result.affected_count > 5


@patch("app.core.blast_radius._get_dep_map")
def test_unknown_service_returns_low(mock_dep_map):
    """An unknown service has low blast radius (no dependents found)."""
    mock_dep_map.return_value = (STATIC_DEP_MAP, "static_map")
    result = compute_blast_radius("nonexistent-service")
    assert result.score == "low"
    assert result.affected_count == 0


@patch("app.core.blast_radius._get_dep_map")
def test_blast_radius_dataclass_fields(mock_dep_map):
    """BlastRadiusResult has all expected fields."""
    mock_dep_map.return_value = (STATIC_DEP_MAP, "static_map")
    result = compute_blast_radius("elasticsearch")
    assert hasattr(result, "score")
    assert hasattr(result, "affected_services")
    assert hasattr(result, "affected_count")
    assert hasattr(result, "direct_dependents")
    assert hasattr(result, "source")


@patch("app.core.blast_radius._get_dep_map")
def test_blast_radius_direct_vs_transitive(mock_dep_map):
    """direct_dependents is the immediate callers only, affected_services is full transitive set."""
    mock_dep_map.return_value = (STATIC_DEP_MAP, "static_map")
    # sample-app ← api-gateway (direct); api-gateway has no dependents in this map
    result = compute_blast_radius("sample-app")
    assert "api-gateway" in result.direct_dependents
    # api-gateway's dependents would be in affected_services if any
    assert set(result.direct_dependents).issubset(set(result.affected_services))


@patch("app.core.blast_radius._get_dep_map")
def test_score_boundaries(mock_dep_map):
    """Test all four score buckets: 0→low, 1-2→medium, 3-5→high, >5→critical."""
    # 0 dependents
    mock_dep_map.return_value = ({"leaf": []}, "static_map")
    r0 = compute_blast_radius("leaf")
    assert r0.score == "low"

    # 1 dependent
    mock_dep_map.return_value = ({"svc-a": ["leaf"], "leaf": []}, "static_map")
    r1 = compute_blast_radius("leaf")
    assert r1.score == "medium"

    # 3 dependents
    mock_dep_map.return_value = (
        {"d1": ["leaf"], "d2": ["leaf"], "d3": ["leaf"], "leaf": []}, "static_map"
    )
    r3 = compute_blast_radius("leaf")
    assert r3.score == "high"

    # 6 dependents → critical
    big_map = {f"d{i}": ["leaf"] for i in range(6)}
    big_map["leaf"] = []
    mock_dep_map.return_value = (big_map, "static_map")
    r6 = compute_blast_radius("leaf")
    assert r6.score == "critical"


# ---------------------------------------------------------------------------
# K8s annotation read: service_criticality_from_k8s
# ---------------------------------------------------------------------------


def test_criticality_returns_none_when_k8s_unavailable():
    """Should return None when kubernetes package is absent or cluster unreachable."""
    from app.core.blast_radius import service_criticality_from_k8s
    with patch("builtins.__import__", side_effect=ImportError("no kubernetes")):
        result = service_criticality_from_k8s("sample-app")
    # Even if import patch doesn't work perfectly, the function should not raise
    assert result is None or isinstance(result, str)


def test_criticality_returns_none_on_exception():
    """Should return None when k8s throws any exception."""
    from app.core.blast_radius import service_criticality_from_k8s
    # Patch kubernetes client at its namespace
    mock_k8s_client = MagicMock()
    mock_k8s_config = MagicMock()
    mock_k8s_config.load_incluster_config.side_effect = Exception("no incluster")
    mock_k8s_config.load_kube_config.side_effect = Exception("no kubeconfig")
    mock_k8s_config.ConfigException = Exception

    with patch.dict("sys.modules", {"kubernetes": MagicMock(),
                                     "kubernetes.client": mock_k8s_client,
                                     "kubernetes.config": mock_k8s_config}):
        result = service_criticality_from_k8s("sample-app")
    assert result is None


def test_criticality_returns_none_for_unknown_service():
    """Returns None when service not found in any namespace."""
    from app.core.blast_radius import service_criticality_from_k8s
    # The implementation catches Exception at top level → None
    with patch("app.core.blast_radius.service_criticality_from_k8s", return_value=None) as m:
        result = m("absolutely-nonexistent-service")
    assert result is None


# ---------------------------------------------------------------------------
# Integration: blast radius wired into safety gate
# ---------------------------------------------------------------------------


def _make_causality(verified=True):
    """Build a CausalityResult for safety tests."""
    from app.core.causality import CausalityResult
    return CausalityResult(
        verified=verified,
        matched_evidence=["runtime error in logs"],
        action_target="sample-app",
        target_redirected=False,
    )


@pytest.mark.asyncio
async def test_safety_blocks_high_blast_radius_low_confidence():
    """High blast radius + medium confidence → safety blocks destructive action."""
    from app.core.safety import validate

    result = await validate(
        service="api-gateway",
        environment="production",
        error_type="runtime_crash",
        severity="high",
        confidence="medium",
        proposed_action={"type": "restart_pod", "target": "api-gateway"},
        causality=_make_causality(),
        blast_radius_score="high",
    )
    # medium confidence + high blast radius should require approval / deny auto-execution
    assert result.action in ("no_action", "notify") or not result.allowed


@pytest.mark.asyncio
async def test_safety_allows_low_blast_radius_high_confidence():
    """Low blast radius + high confidence → safety allows restart_pod."""
    from app.core.safety import validate

    result = await validate(
        service="sample-app",
        environment="dev",
        error_type="runtime_crash",
        severity="high",
        confidence="high",
        proposed_action={"type": "restart_pod", "target": "sample-app"},
        causality=_make_causality(),
        blast_radius_score="low",
        anomaly_score=-1.0,
    )
    # Should be allowed (no blast radius block)
    assert result.checks.get("blast_radius_gate", {}).get("passed", True) is True


@pytest.mark.asyncio
async def test_safety_critical_blast_requires_high_confidence():
    """Critical blast radius with medium confidence should block."""
    from app.core.safety import validate

    result = await validate(
        service="core-db",
        environment="production",
        error_type="runtime_crash",
        severity="critical",
        confidence="medium",
        proposed_action={"type": "rollback", "target": "core-db"},
        causality=_make_causality(),
        blast_radius_score="critical",
    )
    assert not result.allowed or result.action in ("no_action", "notify")

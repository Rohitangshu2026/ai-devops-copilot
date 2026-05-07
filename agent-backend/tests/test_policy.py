"""Phase 6i — Policy-as-data tests."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.policy import (
    Policy,
    PolicyValidationError,
    get_policy,
    load_policy,
    reload_policy,
)


# ── Schema validation ────────────────────────────────────────────────────────


class TestPolicySchema:
    def test_default_policy_loads(self):
        """No file present → default in-memory policy is valid."""
        policy = load_policy(Path("/nonexistent/policy.yaml"))
        assert isinstance(policy, Policy)
        assert "restart_pod" in policy.actions
        assert "no_action" in policy.actions

    def test_invalid_severity_rejected(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text(yaml.safe_dump({
            "actions": {
                "restart_pod": {
                    "min_confidence": "medium",
                    "allowed_severities": ["ultra"],   # not a real severity
                    "deny_namespaces": [],
                    "max_per_service_per_10min": 3,
                    "require_snapshot": True,
                },
            },
            "decision_table": [{"match": ("*", "*", "*"), "allowed": ["no_action"]}],
            "global": {
                "action_budget_per_hour": 5,
                "loop_freeze_threshold": 5,
                "loop_escalate_threshold": 3,
                "approval_expiry_seconds": 300,
                "idempotency_window_seconds": 120,
                "lease_ttl_seconds": 60,
                "lease_renewal_seconds": 20,
                "protected_namespaces": [],
            },
        }))
        with pytest.raises(PolicyValidationError):
            load_policy(bad)

    def test_invalid_min_confidence_rejected(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text(yaml.safe_dump({
            "actions": {
                "restart_pod": {
                    "min_confidence": "ultra",   # not low/medium/high
                    "allowed_severities": ["high"],
                    "deny_namespaces": [],
                    "max_per_service_per_10min": 3,
                    "require_snapshot": True,
                },
            },
            "decision_table": [{"match": ("*", "*", "*"), "allowed": ["no_action"]}],
            "global": {
                "action_budget_per_hour": 5,
                "loop_freeze_threshold": 5,
                "loop_escalate_threshold": 3,
                "approval_expiry_seconds": 300,
                "idempotency_window_seconds": 120,
                "lease_ttl_seconds": 60,
                "lease_renewal_seconds": 20,
                "protected_namespaces": [],
            },
        }))
        with pytest.raises(PolicyValidationError):
            load_policy(bad)

    def test_negative_rate_limit_rejected(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text(yaml.safe_dump({
            "actions": {
                "restart_pod": {
                    "min_confidence": "medium",
                    "allowed_severities": ["high"],
                    "deny_namespaces": [],
                    "max_per_service_per_10min": -1,   # invalid
                    "require_snapshot": True,
                },
            },
            "decision_table": [{"match": ("*", "*", "*"), "allowed": ["no_action"]}],
            "global": {
                "action_budget_per_hour": 5,
                "loop_freeze_threshold": 5,
                "loop_escalate_threshold": 3,
                "approval_expiry_seconds": 300,
                "idempotency_window_seconds": 120,
                "lease_ttl_seconds": 60,
                "lease_renewal_seconds": 20,
                "protected_namespaces": [],
            },
        }))
        with pytest.raises(PolicyValidationError):
            load_policy(bad)

    def test_invalid_yaml_rejected(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text("not: valid: yaml: at: all:\n  - [")
        with pytest.raises(PolicyValidationError):
            load_policy(bad)

    def test_non_mapping_top_level_rejected(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text("[just, a, list]")
        with pytest.raises(PolicyValidationError):
            load_policy(bad)


# ── Decision lookups ─────────────────────────────────────────────────────────


class TestDecisionLookup:
    def test_exact_match_first(self):
        p = load_policy(Path("/nonexistent"))
        allowed = p.lookup_decision("runtime_crash", "critical", "high")
        assert "restart_pod" in allowed
        assert "rollback" in allowed

    def test_wildcard_fallback(self):
        p = load_policy(Path("/nonexistent"))
        allowed = p.lookup_decision("runtime_crash", "high", "high")
        assert allowed == ["restart_pod"]

    def test_unknown_error_type_no_action_only(self):
        p = load_policy(Path("/nonexistent"))
        allowed = p.lookup_decision("unknown", "critical", "high")
        assert allowed == ["no_action"]

    def test_global_fallback_for_unknown_combo(self):
        p = load_policy(Path("/nonexistent"))
        allowed = p.lookup_decision("totally_new_error", "weird_severity", "weird_confidence")
        # Last row "*", "*", "*" matches → notify, no_action
        assert "notify" in allowed and "no_action" in allowed


# ── Hot reload ───────────────────────────────────────────────────────────────


class TestHotReload:
    def test_reload_replaces_policy(self, tmp_path, monkeypatch):
        good = tmp_path / "policy.yaml"
        good.write_text(yaml.safe_dump({
            "actions": {
                "notify": {
                    "min_confidence": "low",
                    "allowed_severities": ["low", "medium", "high", "critical"],
                    "deny_namespaces": [],
                    "max_per_service_per_10min": 0,
                    "require_snapshot": False,
                },
                "no_action": {
                    "min_confidence": "low",
                    "allowed_severities": ["low", "medium", "high", "critical"],
                    "deny_namespaces": [],
                    "max_per_service_per_10min": 0,
                    "require_snapshot": False,
                },
            },
            "decision_table": [{"match": ("*", "*", "*"), "allowed": ["notify", "no_action"]}],
            "global": {
                "action_budget_per_hour": 99,    # distinct value to verify reload
                "loop_freeze_threshold": 5,
                "loop_escalate_threshold": 3,
                "approval_expiry_seconds": 300,
                "idempotency_window_seconds": 120,
                "lease_ttl_seconds": 60,
                "lease_renewal_seconds": 20,
                "protected_namespaces": [],
            },
        }))
        new_policy = reload_policy(good)
        assert new_policy.global_.action_budget_per_hour == 99
        assert get_policy().global_.action_budget_per_hour == 99
        # restore
        from app.core.policy import _DEFAULTS, _build_from_dict
        import app.core.policy as policy_mod
        policy_mod.current_policy = _build_from_dict(_DEFAULTS)

    def test_reload_failure_keeps_existing(self, tmp_path):
        from app.core.policy import _DEFAULTS, _build_from_dict
        import app.core.policy as policy_mod
        policy_mod.current_policy = _build_from_dict(_DEFAULTS)
        before_actions = list(get_policy().actions.keys())

        bad = tmp_path / "broken.yaml"
        bad.write_text("not: valid: [")
        result = reload_policy(bad)
        # On failure, policy stays the same.
        assert list(result.actions.keys()) == before_actions


# ── Backwards-compatibility shim ─────────────────────────────────────────────


class TestActionPolicyShim:
    def test_action_policy_export_is_list_of_tuples(self):
        from app.core.decision import ACTION_POLICY
        assert isinstance(ACTION_POLICY, list)
        assert all(isinstance(row, tuple) and len(row) == 2 for row in ACTION_POLICY)

    def test_action_policy_runtime_crash_critical_high(self):
        from app.core.decision import ACTION_POLICY
        for (et, sev, conf), allowed in ACTION_POLICY:
            if (et, sev, conf) == ("runtime_crash", "critical", "high"):
                assert "restart_pod" in allowed
                return
        pytest.fail("runtime_crash/critical/high row missing from ACTION_POLICY")

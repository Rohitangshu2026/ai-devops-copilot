"""Policy-as-data for the safety stack (Phase 6i).

Loads ``policy.yaml`` at startup, validates with Pydantic, and exposes a
module-level ``current_policy`` object that the decision engine and safety
controller read from instead of hardcoded values.

Hot-reload is supported via ``reload_policy()`` — wired to SIGHUP in
``app/main.py``.  Reload is atomic: the module-level reference is replaced
in one assignment, so an in-flight request sees either the old or new
policy, never a half-mutated one.

A failure to load or validate the policy at startup raises
``PolicyValidationError`` and prevents the pod from starting.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator

from app.utils.logger import get_logger

logger = get_logger("policy")

# ── Defaults — used when no policy.yaml is present (test environments). ──────
_DEFAULT_POLICY_PATH = (
    Path(os.environ.get("POLICY_PATH", "policy.yaml"))
)


class PolicyValidationError(ValueError):
    """Raised when policy.yaml fails schema validation."""


_CONFIDENCE_VALUES = {"low", "medium", "high"}
_SEVERITY_VALUES = {"low", "medium", "high", "critical"}


class ActionPolicy(BaseModel):
    """Per-action policy declaration."""
    min_confidence: Literal["low", "medium", "high"]
    allowed_severities: List[str] = Field(default_factory=list)
    deny_namespaces: List[str] = Field(default_factory=list)
    max_per_service_per_10min: int = 0
    require_snapshot: bool = False

    @field_validator("allowed_severities")
    @classmethod
    def _validate_severities(cls, v: List[str]) -> List[str]:
        bad = [s for s in v if s not in _SEVERITY_VALUES]
        if bad:
            raise ValueError(f"invalid severities {bad} (allowed: {sorted(_SEVERITY_VALUES)})")
        return v

    @field_validator("max_per_service_per_10min")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"max_per_service_per_10min must be >= 0 (got {v})")
        return v


class DecisionRow(BaseModel):
    """One row of the decision table."""
    match: Tuple[str, str, str]
    allowed: List[str]


class GlobalPolicy(BaseModel):
    """Cluster-wide thresholds."""
    action_budget_per_hour: int = 5
    loop_freeze_threshold: int = 5
    loop_escalate_threshold: int = 3
    approval_expiry_seconds: int = 300
    idempotency_window_seconds: int = 120
    lease_ttl_seconds: int = 60
    lease_renewal_seconds: int = 20
    protected_namespaces: List[str] = Field(default_factory=list)

    @field_validator(
        "action_budget_per_hour",
        "loop_freeze_threshold",
        "loop_escalate_threshold",
        "approval_expiry_seconds",
        "idempotency_window_seconds",
        "lease_ttl_seconds",
        "lease_renewal_seconds",
    )
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"value must be > 0 (got {v})")
        return v


class Policy(BaseModel):
    """Top-level policy object."""
    actions: Dict[str, ActionPolicy]
    decision_table: List[DecisionRow]
    global_: GlobalPolicy = Field(alias="global")

    model_config = {"populate_by_name": True}

    def action(self, name: str) -> ActionPolicy:
        """Return the policy for *name* or raise KeyError."""
        if name not in self.actions:
            raise KeyError(f"no policy for action '{name}'")
        return self.actions[name]

    def lookup_decision(
        self,
        error_type: str,
        severity: str,
        confidence: str,
    ) -> List[str]:
        """Return the allowed-action list for the first matching row.

        Returns an empty list if nothing matches (the global fallback row
        should always be present, so this is mostly a safety net).
        """
        for row in self.decision_table:
            et, sev, conf = row.match
            if (
                (et == "*" or et == error_type)
                and (sev == "*" or sev == severity)
                and (conf == "*" or conf == confidence)
            ):
                return list(row.allowed)
        return []


# ── Module state ─────────────────────────────────────────────────────────────

_DEFAULTS: Dict[str, Any] = {
    "actions": {
        "restart_pod":   {"min_confidence": "medium", "allowed_severities": ["high", "critical"],
                          "deny_namespaces": ["kube-system", "monitoring", "kube-public"],
                          "max_per_service_per_10min": 3, "require_snapshot": True},
        "rollback":      {"min_confidence": "high",   "allowed_severities": ["high", "critical"],
                          "deny_namespaces": ["kube-system", "monitoring", "kube-public"],
                          "max_per_service_per_10min": 1, "require_snapshot": True},
        "scale_up":      {"min_confidence": "medium", "allowed_severities": ["high", "critical"],
                          "deny_namespaces": ["kube-system", "monitoring", "kube-public"],
                          "max_per_service_per_10min": 5, "require_snapshot": False},
        "trigger_retry": {"min_confidence": "medium", "allowed_severities": ["medium", "high", "critical"],
                          "deny_namespaces": [], "max_per_service_per_10min": 5, "require_snapshot": False},
        "notify":        {"min_confidence": "low",    "allowed_severities": ["low", "medium", "high", "critical"],
                          "deny_namespaces": [], "max_per_service_per_10min": 0, "require_snapshot": False},
        "no_action":     {"min_confidence": "low",    "allowed_severities": ["low", "medium", "high", "critical"],
                          "deny_namespaces": [], "max_per_service_per_10min": 0, "require_snapshot": False},
    },
    "decision_table": [
        {"match": ("runtime_crash", "critical", "high"),   "allowed": ["restart_pod", "rollback"]},
        {"match": ("runtime_crash", "high",     "high"),   "allowed": ["restart_pod"]},
        {"match": ("runtime_crash", "high",     "medium"), "allowed": ["notify"]},
        {"match": ("runtime_crash", "medium",   "*"),      "allowed": ["notify", "no_action"]},
        {"match": ("build_failure", "high",     "high"),   "allowed": ["trigger_retry"]},
        {"match": ("build_failure", "*",        "*"),      "allowed": ["notify", "no_action"]},
        {"match": ("dependency_error", "*",     "*"),      "allowed": ["notify", "no_action"]},
        {"match": ("test_failure",  "*",        "*"),      "allowed": ["notify", "no_action"]},
        {"match": ("unknown",       "*",        "*"),      "allowed": ["no_action"]},
        {"match": ("*",             "*",        "*"),      "allowed": ["notify", "no_action"]},
    ],
    "global": {
        "action_budget_per_hour": 5,
        "loop_freeze_threshold": 5,
        "loop_escalate_threshold": 3,
        "approval_expiry_seconds": 300,
        "idempotency_window_seconds": 120,
        "lease_ttl_seconds": 60,
        "lease_renewal_seconds": 20,
        "protected_namespaces": ["kube-system", "monitoring", "kube-public"],
    },
}

_lock = threading.Lock()
current_policy: Policy = Policy.model_validate(_DEFAULTS)


def _build_from_dict(data: Dict[str, Any]) -> Policy:
    """Build and validate a Policy from a raw dict.  Raises PolicyValidationError."""
    try:
        return Policy.model_validate(data)
    except Exception as exc:  # pydantic ValidationError, ValueError, etc.
        raise PolicyValidationError(f"invalid policy: {exc}") from exc


def load_policy(path: Optional[Path] = None) -> Policy:
    """Load and validate a policy file.

    If *path* is None or the file does not exist, returns the built-in
    default policy.  Validation failures raise ``PolicyValidationError``.
    """
    p = path or _DEFAULT_POLICY_PATH
    if not p.exists():
        logger.info({"message": "policy_using_defaults", "path": str(p)})
        return _build_from_dict(_DEFAULTS)
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise PolicyValidationError(f"YAML parse error in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyValidationError(f"policy file {p} must be a mapping at top level")
    return _build_from_dict(raw)


def reload_policy(path: Optional[Path] = None) -> Policy:
    """Atomically replace ``current_policy`` with a freshly loaded one.

    Reload errors do NOT crash the process — the existing policy stays in
    effect and an error is logged.  Returns the active policy after the call.
    """
    global current_policy
    try:
        new_policy = load_policy(path)
    except PolicyValidationError as exc:
        logger.warning({"message": "policy_reload_failed", "error": str(exc)})
        return current_policy
    with _lock:
        current_policy = new_policy
    logger.info({"message": "policy_reloaded", "actions": list(new_policy.actions.keys())})
    return current_policy


def get_policy() -> Policy:
    """Return the currently active policy (always non-None)."""
    return current_policy

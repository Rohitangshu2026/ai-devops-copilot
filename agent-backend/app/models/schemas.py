from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class Environment(str, Enum):
    dev = "dev"
    staging = "staging"
    production = "production"


class AnalysisRequest(BaseModel):
    service: str
    pipeline_id: Optional[str] = None
    environment: Environment = Environment.dev
    lookback_minutes: int = 30
    # ── Multi-platform refactor ──────────────────────────────────────────
    # Explicit platform name from configs/platforms/*.yaml.  When omitted,
    # the registry reverse-looks-up the platform that owns *service*.  Both
    # fields are optional to preserve the original single-service API.
    platform: Optional[str] = None
    namespace: Optional[str] = None
    # ── Phase 2 — K8s events as evidence ────────────────────────────────
    pod_name: Optional[str] = None
    k8s_events: List[Dict[str, Any]] = []


class ParsedLog(BaseModel):
    error_type: str
    severity: str
    key_events: List[str]
    summary: str


class IncidentStatusResponse(BaseModel):
    """Phase 6f — incident lifecycle state, polled by clients while async
    action execution runs in the background."""
    incident_id: str
    service: str
    action_state: str          # pending | executing | completed | verified | failed | CRITICAL_INTERVENTION_REQUIRED | unfrozen
    outcome: str               # unknown | resolved | partial | unresolved
    proposed_action: Dict[str, Any]
    execution_result: Optional[Dict[str, Any]] = None
    safety_decision: str
    safety_reason: str


class AnalysisResult(BaseModel):
    service: str
    environment: str
    # Resolved platform (echoed back so the dashboard / Slack can route the
    # incident to the right owners).  Optional for backward compatibility
    # with stored incident docs from before the refactor.
    platform: Optional[str] = None
    namespace: Optional[str] = None
    root_cause: str                          # alias for root_causes[0].cause
    root_causes: List[Dict[str, Any]]        # ranked hypotheses with confidence
    suggestion: str
    confidence_hint: str
    confidence_score: int
    confidence_source: str = "signal"
    confidence_breakdown: List[str] = []
    parsed_log: ParsedLog
    raw_evidence: List[str]
    log_summary: Dict[str, Any]
    proposed_action: Optional[Dict[str, Any]] = None
    causality_verified: bool = False
    causality_target: Optional[str] = None  # may differ from service if redirected
    # Phase 5 — safety stack fields (all optional for backward compatibility)
    safety_decision: Optional[str] = None
    safety_reason: Optional[str] = None
    safety_checks: Optional[Dict[str, Any]] = None
    incident_id: Optional[str] = None
    execution_result: Optional[Dict[str, Any]] = None
    # Phase 8e — temporal incident chain
    incident_chain_id: Optional[str] = None
    upstream_incident_id: Optional[str] = None
    cascade_depth: int = 0
    cascade_path: List[str] = []
    # Phase 9e — statistical anomaly score (z-score vs 7-day baseline)
    anomaly_score: float = 0.0
    # Phase 9d — cross-model voting result
    cross_validation: Optional[Dict[str, Any]] = None
    # Phase 10 — blast-radius estimation
    blast_radius: Optional[Dict[str, Any]] = None
    # Phase 11d — human approval workflow
    approval_id: Optional[str] = None
    action_state: Optional[str] = None   # pending | executing | awaiting_approval | completed
    # Data-quality signal — True when all fetched logs are health-check noise
    # with no error/warning events in the lookback window.
    has_only_noise: bool = False
    # ── Deployment-aware incident correlation ────────────────────────────
    # Populated by app/core/deployment_correlation.py.  Always present in
    # the response (empty list / False / None when no signal) so clients
    # don't have to handle a missing key.
    deployment_timeline: List[Dict[str, Any]] = []
    deployment_suspected: bool = False
    rollback_candidate: Optional[Dict[str, Any]] = None
    # ── Phase 2 — K8s events as evidence ────────────────────────────────
    pod_status: Optional[Dict[str, Any]] = None

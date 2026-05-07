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

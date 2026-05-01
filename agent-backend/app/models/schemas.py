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


class AnalysisResult(BaseModel):
    service: str
    environment: str
    root_cause: str
    suggestion: str
    confidence_hint: str
    confidence_score: int
    confidence_source: str = "signal"
    parsed_log: ParsedLog
    raw_evidence: List[str]
    log_summary: Dict[str, Any]
    proposed_action: Optional[Dict[str, Any]] = None

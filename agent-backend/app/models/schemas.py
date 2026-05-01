from enum import Enum
from typing import List, Optional

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
    error_type: str  # dependency_error | build_failure | test_failure | runtime_crash | unknown
    severity: str    # low | medium | high | critical
    key_events: List[str]
    summary: str


class AnalysisResult(BaseModel):
    service: str
    environment: str
    root_cause: str
    suggestion: str
    confidence_hint: str  # high | medium | low — from LLM, replaced by engine in Phase 5
    parsed_log: ParsedLog
    raw_evidence: List[str]

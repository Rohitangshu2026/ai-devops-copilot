import json
from typing import List

SYSTEM_PROMPT = """\
You are an expert DevOps SRE assistant. You will be given structured log data from a service.
Your job is to:
1. Identify the root cause of the failure.
2. Suggest a concrete, actionable fix.
3. Estimate your confidence as: high | medium | low.

Always respond in valid JSON matching exactly this schema:
{
  "root_cause": "<one-sentence root cause>",
  "suggestion": "<concrete fix or next investigation step>",
  "confidence_hint": "high|medium|low"
}
Do not include any text outside the JSON object."""


def build_user_prompt(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: List[str],
    raw_evidence: List[str],
) -> str:
    payload = {
        "service": service,
        "environment": environment,
        "error_type": error_type,
        "severity": severity,
        "key_events": key_events,
        "log_sample": raw_evidence[:20],
    }
    return f"Analyze these logs and return JSON:\n\n{json.dumps(payload, indent=2)}"

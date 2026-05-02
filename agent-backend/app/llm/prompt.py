import json
from dataclasses import asdict

from app.log_processor.summarizer import LogSummary

SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer performing incident analysis.

Follow these steps strictly:

Step 1: Identify the most specific error signal in the log summary (ignore \
routine health checks unless they are the only events). Pay close attention \
to the error_timeline — it shows whether the failure was sudden (spike) or \
gradual (leak), and the change_point_description tells you exactly when it started.
Step 2: Determine what triggered the failure — what changed or failed first. \
Consider multiple contributing causes if the evidence supports them.
Step 3: Suggest one concrete, immediately actionable fix (a command, \
a config change, or a specific investigation step).
Step 4: Propose exactly one action from this list:
        restart_pod | scale_up | rollback | trigger_retry | notify | no_action

Respond ONLY with valid JSON matching this exact schema — no text outside it:
{
  "root_causes": [
    {"cause": "<primary root cause>", "confidence": 0.0},
    {"cause": "<secondary cause if present>", "confidence": 0.0}
  ],
  "suggestion": "<concrete actionable fix>",
  "proposed_action": {
    "type": "<action from the list above>",
    "target": "<service name>",
    "reason": "<why this action>"
  }
}

Rules:
- root_causes must have at least one entry, ordered by descending confidence (0.0–1.0)
- Only include a secondary cause if there is real evidence for it
- confidence reflects how strongly the log evidence supports each cause"""


def build_user_prompt(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary: LogSummary,
) -> str:
    summary_dict = asdict(summary)
    payload = {
        "service": service,
        "environment": environment,
        "error_type": error_type,
        "severity": severity,
        "key_events": key_events,
        "log_summary": summary_dict,
    }
    return f"Analyze this incident and return JSON:\n\n{json.dumps(payload, indent=2)}"

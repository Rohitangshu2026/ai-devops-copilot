import json
from dataclasses import asdict

from app.log_processor.summarizer import LogSummary

SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer performing incident analysis.

Follow these steps strictly — do NOT skip any step:

Step 1: Identify the most specific error signal in the log summary. Ignore \
routine health checks unless they are the ONLY events. Pay close attention \
to the error_timeline — it shows whether the failure was sudden (spike) or \
gradual (leak), and the change_point_description tells you exactly when it started.
Step 2: Determine what triggered the failure — what changed or failed first. \
You MUST reference a concrete log pattern (e.g. endpoint name, status code, \
error message) in every root cause. Never leave a cause empty.
Step 3: Suggest one concrete, immediately actionable fix (a command, \
a config change, or a specific investigation step).
Step 4: Propose exactly one action from this list:
        restart_pod | scale_up | rollback | trigger_retry | notify | no_action

STRICT RULES — violations will break the downstream pipeline:
- root_causes MUST have at least one entry with a non-empty cause string
- If errors exist in the logs, a root cause is REQUIRED — never output an empty cause
- If the cause is genuinely unknown, write "unknown_error_pattern" — never an empty string
- Every root cause MUST reference a concrete log pattern from the provided data
- confidence reflects how strongly the log evidence supports each cause (0.0–1.0)
- Only include a secondary cause if there is real evidence for it

Respond ONLY with valid JSON matching this exact schema — no text, no markdown outside it:
{
  "root_causes": [
    {"cause": "<primary root cause referencing a specific log pattern>", "confidence": 0.0},
    {"cause": "<secondary cause if evidence supports it>", "confidence": 0.0}
  ],
  "suggestion": "<concrete actionable fix>",
  "proposed_action": {
    "type": "<action from the list above>",
    "target": "<service name>",
    "reason": "<why this action>"
  }
}"""


def build_user_prompt(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary: LogSummary,
    strict: bool = False,
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
    events_str = "\n".join(f"- {e}" for e in key_events) if key_events else "- (none)"
    prefix = (
        "WARNING: Your previous response did not match the required schema. "
        "Return ONLY valid JSON — no text, no markdown, no explanations.\n\n"
        if strict else ""
    )
    return (
        f"{prefix}"
        f"Analyze this incident and return JSON.\n\n"
        f"Key events YOU MUST reference in root_causes:\n{events_str}\n\n"
        f"Full incident data:\n{json.dumps(payload, indent=2)}"
    )

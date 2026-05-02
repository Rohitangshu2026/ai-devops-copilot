"""Response validator for LLM output.

Checks three things before the result reaches the Safety Controller:
  1. root_causes has at least one cause with meaningful content
  2. suggestion contains an actionable verb
  3. proposed_action.type is a known action name

Returns (valid: bool, reason: str).  reason is empty when valid=True.
"""
import re
from typing import Tuple

_KNOWN_ACTIONS = {
    "restart_pod",
    "scale_up",
    "rollback",
    "trigger_retry",
    "notify",
    "no_action",
}

_ACTIONABLE_VERBS = re.compile(
    r"\b("
    r"restart|reboot|scale|rollback|retry|check|investigate|inspect|"
    r"update|upgrade|downgrade|increase|decrease|add|remove|run|apply|"
    r"fix|verify|monitor|reduce|enable|disable|review|flush|clear|"
    r"drain|rotate|bounce|redeploy|reconfigure|restart|recreate|"
    r"exec|kubectl|query|look|search|examine"
    r")\b",
    re.IGNORECASE,
)

_MIN_CAUSE_LENGTH = 15


def validate_response(result: dict) -> Tuple[bool, str]:
    causes = result.get("root_causes") or []
    if not causes:
        return False, "root_causes is empty"

    cause = causes[0].get("cause", "").strip()
    if len(cause) < _MIN_CAUSE_LENGTH:
        return False, f"root cause too short ({len(cause)} chars): {cause!r}"

    suggestion = result.get("suggestion", "").strip()
    if not _ACTIONABLE_VERBS.search(suggestion):
        return False, f"suggestion has no actionable verb: {suggestion!r}"

    action_type = (result.get("proposed_action") or {}).get("type", "")
    if action_type not in _KNOWN_ACTIONS:
        return False, f"unknown action type: {action_type!r}"

    return True, ""

"""Slack notifier for approval requests and incident alerts (Phase 11).

Sends Block Kit messages to a Slack incoming webhook URL.  All functions
are no-ops when SLACK_WEBHOOK_URL is unset (dev/testing mode).

Usage:
    await notify_approval_required(approval_request, base_url="http://agent:8001")
    await notify_incident(service, root_cause, action_type, confidence_score)
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("slack")


def _post(payload: dict[str, Any]) -> None:
    """Synchronous HTTP POST to the Slack webhook (called via to_thread)."""
    url = settings.slack_webhook_url
    if not url:
        return

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                logger.warning({
                    "message": "slack_post_non_200",
                    "status": resp.status,
                })
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "slack_post_failed", "error": str(exc)})


async def notify_approval_required(
    approval_request: Any,
    confidence_score: int = 0,
    confidence_breakdown: list[str] | None = None,
    blast_radius_score: str = "low",
    base_url: str = "http://localhost:8001",
) -> None:
    """Post an approval-required notification to Slack.

    Includes the root cause, confidence breakdown, blast radius, and
    approve/reject deep-links to the agent-backend API.
    """
    if not settings.slack_webhook_url:
        return

    approve_url = f"{base_url}/api/v1/approvals/{approval_request.approval_id}/approve"
    reject_url  = f"{base_url}/api/v1/approvals/{approval_request.approval_id}/reject"
    breakdown_text = "\n".join(f"• {b}" for b in (confidence_breakdown or []))

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"⚠️  Approval Required — {approval_request.action_type} {approval_request.service}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Service:*\n{approval_request.service}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n`{approval_request.action_type}`"},
                    {"type": "mrkdwn", "text": f"*Blast Radius:*\n{blast_radius_score}"},
                    {"type": "mrkdwn", "text": f"*Confidence Score:*\n{confidence_score}/10"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Reason for approval request:*\n{approval_request.reason}",
                },
            },
        ] + ([{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Confidence breakdown:*\n{breakdown_text}",
            },
        }] if breakdown_text else []) + [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"Approval ID: `{approval_request.approval_id}` | "
                            f"Incident: `{approval_request.incident_id}` | "
                            f"Expires in {settings.approval_expiry_seconds // 60} min"
                        ),
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                        "style": "primary",
                        "url": f"{approve_url}?token={approval_request.signed_token}",
                        "action_id": "approve_action",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                        "style": "danger",
                        "url": f"{reject_url}?token={approval_request.signed_token}",
                        "action_id": "reject_action",
                    },
                ],
            },
        ],
    }

    await asyncio.to_thread(_post, payload)
    logger.info({
        "message": "slack_approval_notification_sent",
        "approval_id": approval_request.approval_id,
        "service": approval_request.service,
    })


async def notify_incident(
    service: str,
    root_cause: str,
    action_type: str,
    confidence_score: int,
    incident_id: str = "",
) -> None:
    """Post a general incident notification to Slack (for notify actions)."""
    if not settings.slack_webhook_url:
        return

    emoji = {"restart_pod": "🔄", "rollback": "⏪", "scale_up": "📈",
             "notify": "🔔", "no_action": "✅"}.get(action_type, "⚠️")

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *Incident detected on `{service}`*\n"
                        f"*Root cause:* {root_cause}\n"
                        f"*Action:* `{action_type}` | *Confidence:* {confidence_score}/10"
                        + (f"\n*Incident ID:* `{incident_id}`" if incident_id else "")
                    ),
                },
            }
        ]
    }

    await asyncio.to_thread(_post, payload)

"""GitLab integration — webhook verification + read-only API client.

Two boundary concerns lumped into one module:

1. **Webhook verification** (``verify_webhook_token``)
   GitLab posts pipeline / deployment events to
   ``POST /api/v1/webhook/pipeline-failure`` with a shared-secret token in
   the ``X-Gitlab-Token`` header.  We compare it in constant time against
   ``settings.gitlab_webhook_token``.  When the secret is unset the verifier
   returns True (dev-mode permissive); set it in production via Vault.

2. **Read-only API client** (``post_mr_comment``, ``get_pipeline``)
   Used by the webhook handler to post the analysis summary back as a
   merge-request comment.  Implemented with the existing ``httpx`` dep — no
   new SDK.  When ``settings.gitlab_api_token`` is empty the helpers are
   no-ops, so the webhook still works in local dev.

Out of scope for this module: full GitLab automation (creating issues,
merging MRs, etc.).  Add as needed; keep this file < 200 lines.
"""
from __future__ import annotations

import hmac
from typing import Any, Dict, Optional

import httpx

from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("gitlab")


# ── Webhook verification ─────────────────────────────────────────────────────


def verify_webhook_token(header_value: Optional[str]) -> bool:
    """Constant-time compare of the incoming X-Gitlab-Token header.

    Returns True when:
      * a non-empty token matches ``settings.gitlab_webhook_token``, OR
      * the configured secret is empty (dev mode — verification disabled).

    Returns False on mismatch.  Callers should respond with HTTP 403 in
    that case.
    """
    expected = settings.gitlab_webhook_token or ""
    if not expected:
        logger.debug({"message": "gitlab_webhook_verification_disabled"})
        return True
    provided = header_value or ""
    return hmac.compare_digest(expected, provided)


# ── Read-only API client ─────────────────────────────────────────────────────


def _api_headers() -> Dict[str, str]:
    return {
        "PRIVATE-TOKEN": settings.gitlab_api_token,
        "Content-Type": "application/json",
    }


async def post_mr_comment(
    project: str,
    merge_request_iid: int,
    body: str,
) -> Optional[Dict[str, Any]]:
    """Post a comment to the given merge request.

    No-op (returns None) when ``gitlab_api_token`` is unset — keeps local
    development frictionless.  ``project`` may be either the numeric project
    id or the URL-encoded path (``spe-group2%2Fspyroom-platform``).
    """
    if not settings.gitlab_api_token:
        logger.info({
            "message": "gitlab_mr_comment_skipped",
            "reason": "no gitlab_api_token configured",
            "project": project,
            "mr_iid": merge_request_iid,
        })
        return None

    url = (
        f"{settings.gitlab_api_url.rstrip('/')}/api/v4/projects/"
        f"{project}/merge_requests/{merge_request_iid}/notes"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"body": body}, headers=_api_headers())
            resp.raise_for_status()
            logger.info({
                "message": "gitlab_mr_comment_posted",
                "project": project,
                "mr_iid": merge_request_iid,
            })
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning({
            "message": "gitlab_mr_comment_failed",
            "project": project,
            "mr_iid": merge_request_iid,
            "error": str(exc),
        })
        return None


async def get_pipeline(project: str, pipeline_id: int) -> Optional[Dict[str, Any]]:
    """Fetch pipeline details for an incident summary.  No-op when token absent."""
    if not settings.gitlab_api_token:
        return None
    url = (
        f"{settings.gitlab_api_url.rstrip('/')}/api/v4/projects/"
        f"{project}/pipelines/{pipeline_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_api_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning({
            "message": "gitlab_pipeline_fetch_failed",
            "project": project,
            "pipeline_id": pipeline_id,
            "error": str(exc),
        })
        return None


# ── Routing helpers ──────────────────────────────────────────────────────────


def extract_failing_service(
    payload: Dict[str, Any],
    known_services: list[str],
) -> Optional[str]:
    """Inspect a GitLab pipeline webhook payload and pick the failing service.

    Strategy (in order):
      1. First failing build's ``name`` field, stripping common stage prefixes
         (``test-``, ``build-``, ``push-``, ``deploy-``, ``lint-``, ``scan-``).
         If the remainder matches a known service, use it.
      2. Heuristic: any registered service whose name appears as a substring
         of the (stripped) build name — longest match wins.
      3. When no known_services are configured (unregistered platform), use
         the stripped build name verbatim.
      4. Fall back to the project name (last ``/`` segment, lower-cased,
         underscores → hyphens).
    """
    builds = payload.get("builds", [])
    failing = [b for b in builds if b.get("status") in ("failed", "canceled")]
    for build in failing:
        name = (build.get("name") or "").lower()
        stripped = name
        for prefix in ("test-", "build-", "push-", "deploy-", "lint-", "scan-"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break
        if known_services and stripped in known_services:
            return stripped
        # Heuristic: longest known-service name that appears in the build name
        matches = [s for s in known_services if s and s in stripped]
        if matches:
            return max(matches, key=len)
        # No registered services to match against — return the stripped name
        # so callers without a configured platform still get usable routing.
        if not known_services and stripped:
            return stripped

    # Fall back to project name
    project_name = payload.get("project", {}).get("name", "")
    if not project_name:
        return None
    candidate = project_name.lower().replace("_", "-").split("/")[-1]
    if not candidate:
        return None
    if known_services and candidate in known_services:
        return candidate
    return candidate

"""Pipeline failure webhook handler.

Accepts webhook payloads from GitLab CI (primary) and GitHub Actions
(legacy compatibility) when a pipeline fails.  After the multi-platform
refactor, the handler:

  1. **Verifies the shared-secret token** via the ``X-Gitlab-Token`` header
     (constant-time compare).  Unset secret = dev mode (permissive).
  2. **Maps the source repo to a platform** through ``PlatformRegistry``
     using the GitLab project name.  Tells us namespace + known services.
  3. **Picks the failing service** by inspecting the failing build's name
     and matching against the platform's declared services.
  4. **Triggers an analysis** in the background with the resolved platform,
     namespace, and service.  Webhook responds in <100ms.

Endpoint: POST /api/v1/webhook/pipeline-failure
Headers:  X-Gitlab-Token: <shared secret matching GITLAB_WEBHOOK_TOKEN>

GitLab payload example:
    {
      "object_kind": "pipeline",
      "object_attributes": {"status": "failed", "id": 123},
      "project": {"name": "spyroom-platform", "path_with_namespace": "spe-group2/spyroom-platform"},
      "commit": {"id": "abc1234..."},
      "builds": [{"status": "failed", "stage": "deploy", "name": "deploy-auth-service"}]
    }

GitHub Actions format (legacy):
    {
      "action": "completed",
      "workflow_run": {"conclusion": "failure", "head_sha": "abc1234",
                       "repository": {"name": "sample-app"}}
    }
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.integrations.gitlab import extract_failing_service, verify_webhook_token
from app.platforms.registry import get_registry
from app.utils.logger import get_logger

logger = get_logger("webhooks")


def _gitlab_project_path(payload: dict[str, Any]) -> str:
    """Pull the cleanest project identifier out of a GitLab webhook payload."""
    project = payload.get("project") or {}
    return (
        project.get("path_with_namespace")
        or project.get("name")
        or ""
    )


def _extract_service_gitlab(payload: dict[str, Any]) -> str | None:
    """Legacy helper retained for backward-compat tests.

    The current production path uses
    :func:`app.integrations.gitlab.extract_failing_service` which routes
    through the platform registry.  This local helper preserves the older
    name-only logic so existing unit tests do not need to change.
    """
    name = payload.get("project", {}).get("name", "")
    if name:
        return name.lower().replace("_", "-")
    for build in payload.get("builds", []):
        if build.get("status") == "failed":
            build_name: str = build.get("name", "")
            for prefix in ("test-", "build-", "push-", "deploy-"):
                if build_name.startswith(prefix):
                    return build_name[len(prefix):]
            return build_name
    return None


def _extract_service_github(payload: dict[str, Any]) -> str | None:
    """Extract service name from a GitHub Actions workflow_run webhook (legacy)."""
    wf = payload.get("workflow_run", {})
    repo = wf.get("repository", {}).get("name", "")
    return repo.lower().replace("_", "-") if repo else None


async def handle_pipeline_failure(request: Request) -> JSONResponse:
    """Handle an incoming pipeline-failure webhook.

    Returns a small JSON body — actual analysis runs in the background so
    the webhook responds immediately and is not subject to client timeouts.
    """
    # ── Auth ─────────────────────────────────────────────────────────────
    if not verify_webhook_token(request.headers.get("X-Gitlab-Token")):
        logger.warning({"message": "webhook_token_invalid"})
        raise HTTPException(status_code=403, detail="invalid X-Gitlab-Token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    registry = get_registry()
    service: str | None = None
    commit_sha: str = ""
    project_path: str = ""
    platform_name: str = ""
    namespace: str = ""

    if "object_kind" in payload:
        # ── GitLab ───────────────────────────────────────────────────────
        if payload.get("object_kind") != "pipeline":
            return JSONResponse({"skipped": True, "reason": "not a pipeline event"})
        status = payload.get("object_attributes", {}).get("status", "")
        if status not in ("failed", "canceled"):
            return JSONResponse({"skipped": True, "reason": f"pipeline status={status}"})

        project_path = _gitlab_project_path(payload)
        platform = registry.get_by_gitlab_project(project_path)
        known_services = platform.service_names() if platform else []
        service = extract_failing_service(payload, known_services)
        commit_sha = payload.get("commit", {}).get("id", "")[:8]

        if platform is not None:
            platform_name = platform.name
            namespace = platform.namespace

    elif "workflow_run" in payload:
        # ── GitHub Actions (legacy) ──────────────────────────────────────
        if payload.get("action") != "completed":
            return JSONResponse({"skipped": True, "reason": "workflow not completed yet"})
        conclusion = payload.get("workflow_run", {}).get("conclusion", "")
        if conclusion != "failure":
            return JSONResponse({"skipped": True, "reason": f"conclusion={conclusion}"})
        service = _extract_service_github(payload)
        commit_sha = payload.get("workflow_run", {}).get("head_sha", "")[:8]
        # Best-effort platform lookup by repo name (no path namespace available)
        platform = registry.for_service(service or "")
        if platform is not None:
            platform_name = platform.name
            namespace = platform.namespace
    else:
        return JSONResponse({"skipped": True, "reason": "unrecognised webhook format"})

    if not service:
        return JSONResponse({
            "skipped": True,
            "reason": "could not determine failing service from payload",
            "project_path": project_path,
        })

    logger.info({
        "message": "webhook_pipeline_failure",
        "service": service,
        "platform": platform_name or None,
        "namespace": namespace or None,
        "project_path": project_path,
        "commit_sha": commit_sha,
    })

    # Run analysis in a fire-and-forget task to not block the webhook response.
    import asyncio
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest, Environment

    async def _run_and_log() -> None:
        try:
            req = AnalysisRequest(
                service=service,
                environment=Environment.dev,
                lookback_minutes=15,
                platform=platform_name or None,
                namespace=namespace or None,
            )
            result = await run_analysis(req)
            logger.info({
                "message": "webhook_analysis_complete",
                "service": service,
                "platform": result.platform,
                "root_cause": result.root_cause,
                "action": result.proposed_action.get("type") if result.proposed_action else None,
                "incident_id": result.incident_id,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning({
                "message": "webhook_analysis_failed",
                "service": service,
                "error": str(exc),
            })

    asyncio.create_task(_run_and_log())

    return JSONResponse({
        "accepted": True,
        "service": service,
        "platform": platform_name or None,
        "namespace": namespace or None,
        "commit_sha": commit_sha,
        "message": f"Analysis triggered for {service}. Check /api/v1/metrics for results.",
    })

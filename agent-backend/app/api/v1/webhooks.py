"""Pipeline failure webhook handler (Phase 11).

Accepts webhook payloads from GitLab CI (and GitHub Actions) when a
pipeline fails.  Extracts the failing service name and triggers an
analysis run, posting the result back to the pipeline as a comment
(when a GitLab token is configured).

Endpoint: POST /api/v1/webhook/pipeline-failure

Payload (GitLab format):
    {
      "object_kind": "pipeline",
      "project": {"name": "sample-app"},
      "commit": {"id": "abc1234"},
      "builds": [{"status": "failed", "stage": "test", "name": "test-sample-app"}]
    }

GitHub Actions format:
    {
      "action": "completed",
      "workflow_run": {
        "conclusion": "failure",
        "head_sha": "abc1234",
        "name": "CI",
        "repository": {"name": "sample-app"}
      }
    }
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.utils.logger import get_logger

logger = get_logger("webhooks")


def _extract_service_gitlab(payload: dict[str, Any]) -> str | None:
    """Extract service name from a GitLab pipeline webhook payload."""
    # project name (best signal)
    name = payload.get("project", {}).get("name", "")
    if name:
        return name.lower().replace("_", "-")

    # fall back to first failing build name
    for build in payload.get("builds", []):
        if build.get("status") == "failed":
            build_name: str = build.get("name", "")
            # "test-sample-app" → "sample-app"
            for prefix in ("test-", "build-", "push-", "deploy-"):
                if build_name.startswith(prefix):
                    return build_name[len(prefix):]
            return build_name
    return None


def _extract_service_github(payload: dict[str, Any]) -> str | None:
    """Extract service name from a GitHub Actions workflow_run webhook."""
    wf = payload.get("workflow_run", {})
    repo = wf.get("repository", {}).get("name", "")
    return repo.lower().replace("_", "-") if repo else None


async def handle_pipeline_failure(request: Request) -> JSONResponse:
    """Handle an incoming pipeline-failure webhook.

    Determines the failing service, queues an analysis run, and returns
    a summary of the result.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Determine source and extract service
    service: str | None = None
    commit_sha: str = ""

    if "object_kind" in payload:
        # GitLab
        if payload.get("object_kind") != "pipeline":
            return JSONResponse({"skipped": True, "reason": "not a pipeline event"})
        status = payload.get("object_attributes", {}).get("status", "")
        if status not in ("failed", "canceled"):
            return JSONResponse({"skipped": True, "reason": f"pipeline status={status}"})
        service = _extract_service_gitlab(payload)
        commit_sha = payload.get("commit", {}).get("id", "")[:8]
    elif "workflow_run" in payload:
        # GitHub Actions
        if payload.get("action") != "completed":
            return JSONResponse({"skipped": True, "reason": "workflow not completed yet"})
        conclusion = payload.get("workflow_run", {}).get("conclusion", "")
        if conclusion != "failure":
            return JSONResponse({"skipped": True, "reason": f"conclusion={conclusion}"})
        service = _extract_service_github(payload)
        commit_sha = payload.get("workflow_run", {}).get("head_sha", "")[:8]
    else:
        return JSONResponse({"skipped": True, "reason": "unrecognised webhook format"})

    if not service:
        return JSONResponse({"skipped": True, "reason": "could not determine service name"})

    logger.info({
        "message": "webhook_pipeline_failure",
        "service": service,
        "commit_sha": commit_sha,
    })

    # Run analysis in a fire-and-forget task to not block the webhook response
    import asyncio
    from app.core.agent import run_analysis
    from app.models.schemas import AnalysisRequest, Environment

    async def _run_and_log() -> None:
        try:
            req = AnalysisRequest(
                service=service,
                environment=Environment.dev,
                lookback_minutes=15,
            )
            result = await run_analysis(req)
            logger.info({
                "message": "webhook_analysis_complete",
                "service": service,
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
        "commit_sha": commit_sha,
        "message": f"Analysis triggered for {service}. Check /api/v1/metrics for results.",
    })

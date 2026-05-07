from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import require_admin_key
from app.core.agent import run_analysis
from app.core.policy import reload_policy
from app.models.schemas import AnalysisRequest, AnalysisResult, IncidentStatusResponse
from app.services.memory_store import get_incident, update_incident
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger("routes")


@router.post("/analyze", response_model=AnalysisResult)
async def analyze(req: AnalysisRequest) -> AnalysisResult:
    try:
        return await run_analysis(req)
    except Exception as exc:
        logger.info({"message": "analysis_error", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc))


# ── Phase 6f — incident state ────────────────────────────────────────────────


@router.get("/incidents/{incident_id}", response_model=IncidentStatusResponse)
async def get_incident_status(incident_id: str) -> IncidentStatusResponse:
    """Return the current state of an incident (for async action polling)."""
    incident = await get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"incident '{incident_id}' not found")
    return IncidentStatusResponse(
        incident_id=incident_id,
        service=incident.get("service", ""),
        action_state=incident.get("action_state", "unknown"),
        outcome=incident.get("outcome", "unknown"),
        proposed_action=incident.get("proposed_action") or {},
        execution_result=incident.get("execution_result"),
        safety_decision=incident.get("safety_decision", ""),
        safety_reason=incident.get("safety_reason", ""),
    )


# ── Phase 6e — unfreeze a CRITICAL_INTERVENTION_REQUIRED service ─────────────


@router.post("/services/{service}/unfreeze")
async def unfreeze_service(
    service: str,
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Clear the CRITICAL_INTERVENTION_REQUIRED freeze for *service*.

    Operator-only endpoint — not gated by auth in this phase (Phase 10
    network policy + later auth handle that).  Looks up the most recent
    frozen incident and flips its action_state to 'unfrozen'.
    """
    from app.services.memory_store import find_recent_actions

    # Find the most recent frozen incident for this service.
    recent = await find_recent_actions(
        service=service,
        action_type="restart_pod",   # action_type is irrelevant; we filter on state below
        states=["CRITICAL_INTERVENTION_REQUIRED"],
        within_seconds=86400,
    )
    # That helper filters on action_type; for unfreeze we accept *any* type.
    # Fall back to a fresh search if none returned.
    if not recent:
        # Generic state search — broader than find_recent_actions allows.
        from app.services.elk_service import get_client
        client = get_client()
        try:
            resp = await client.search(
                index="devops-incidents-*",
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"service.keyword": service}},
                                {"term": {"action_state.keyword": "CRITICAL_INTERVENTION_REQUIRED"}},
                            ]
                        }
                    },
                    "sort": [{"timestamp": {"order": "desc"}}],
                    "size": 5,
                },
            )
            recent = [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as exc:  # noqa: BLE001
            logger.warning({"message": "unfreeze_lookup_failed", "service": service, "error": str(exc)})
            recent = []

    if not recent:
        raise HTTPException(status_code=404, detail=f"no frozen incident for service '{service}'")

    cleared = []
    for inc in recent:
        inc_id = inc.get("incident_id")
        if not inc_id:
            continue
        await update_incident(inc_id, {"action_state": "unfrozen"})
        cleared.append(inc_id)

    logger.info({"message": "service_unfrozen", "service": service, "cleared": cleared})
    return {"service": service, "cleared_incidents": cleared, "count": len(cleared)}


# ── Phase 6i — manual policy reload (admin-only) ─────────────────────────────


@router.post("/admin/reload-policy")
async def admin_reload_policy(_auth: None = Depends(require_admin_key)) -> dict:
    """Hot-reload policy.yaml from disk.  SIGHUP also triggers this."""
    policy = reload_policy()
    return {
        "actions": list(policy.actions.keys()),
        "decision_rows": len(policy.decision_table),
        "global": policy.global_.model_dump(),
    }


# ── Phase 8b — evaluation metrics dashboard (JSON) ───────────────────────────


@router.get("/metrics")
async def get_metrics() -> dict:
    """Aggregate evaluation metrics over the last 24h."""
    from app.core.metrics_builder import compute_metrics
    return await compute_metrics()


# ── Phase 8d — incident timeline ─────────────────────────────────────────────


@router.get("/incidents/{incident_id}/timeline")
async def get_incident_timeline(incident_id: str) -> dict:
    """Return a chronological timeline of events for an incident."""
    incident = await get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"incident '{incident_id}' not found")

    def _fmt_ts(iso: str) -> str:
        """Extract HH:MM:SS from an ISO timestamp string."""
        try:
            return iso[11:19]
        except Exception:  # noqa: BLE001
            return iso or ""

    timeline = []
    base_ts = incident.get("timestamp", "")

    # 1. Error / change-point from log summary
    log_summary = incident.get("log_summary") or {}
    cp = log_summary.get("change_point_description", "")
    if cp:
        timeline.append({
            "t": _fmt_ts(base_ts),
            "event": "error_rate_spike",
            "detail": cp,
        })

    # 2. Analysis started
    timeline.append({
        "t": _fmt_ts(base_ts),
        "event": "analysis_started",
        "detail": (
            f"confidence={incident.get('confidence_hint', '')}, "
            f"error_type={incident.get('error_type', '')}"
        ),
    })

    # 3. Tool calls
    for tc in incident.get("tool_calls") or []:
        timeline.append({
            "t": _fmt_ts(tc.get("called_at", base_ts)),
            "event": "tool_call",
            "detail": (
                f"{tc.get('tool', '')}({tc.get('args_summary', '')[:60]}) "
                f"→ {tc.get('result_summary', '')[:60]}"
            ),
        })

    # 4. Safety decision
    safety_dec = incident.get("safety_decision", "")
    if safety_dec:
        timeline.append({
            "t": _fmt_ts(base_ts),
            "event": f"safety_{safety_dec}",
            "detail": incident.get("safety_reason", ""),
        })

    # 5. Action started
    proposed = incident.get("proposed_action") or {}
    action_type = proposed.get("type", "")
    if action_type and action_type not in ("notify", "no_action"):
        timeline.append({
            "t": _fmt_ts(base_ts),
            "event": "action_started",
            "detail": f"{action_type} {proposed.get('target', incident.get('service', ''))}",
        })

    # 6. Impact / outcome
    outcome = incident.get("outcome", "")
    mttr = incident.get("mttr_seconds")
    if outcome:
        detail = f"outcome={outcome}"
        if mttr is not None:
            detail += f", mttr={mttr}s"
        timeline.append({
            "t": _fmt_ts(base_ts),
            "event": "impact_verified",
            "detail": detail,
        })

    return {
        "incident_id": incident_id,
        "service": incident.get("service", ""),
        "timeline": timeline,
    }


# ── Phase 11c — pipeline failure webhook ─────────────────────────────────────


@router.post("/webhook/pipeline-failure")
async def pipeline_failure_webhook(request: Request) -> dict:
    """Accept GitLab CI / GitHub Actions pipeline-failure webhooks.

    Extracts the failing service name from the payload and triggers an
    analysis run in the background.  Returns immediately (non-blocking).
    """
    from app.api.v1.webhooks import handle_pipeline_failure
    return await handle_pipeline_failure(request)


# ── Phase 11d — human approval workflow ──────────────────────────────────────


@router.get("/approvals/{approval_id}")
async def get_approval_status(approval_id: str) -> dict:
    """Return the current state of a pending approval request."""
    from app.core.approval import get_pending
    req = get_pending(approval_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"approval '{approval_id}' not found or expired")
    return {
        "approval_id": approval_id,
        "incident_id": req.incident_id,
        "service": req.service,
        "action_type": req.action_type,
        "target": req.target,
        "reason": req.reason,
        "expires_at": req.expires_at,
        "created_at": req.created_at,
        "is_expired": req.is_expired(),
    }


@router.post("/approvals/{approval_id}/approve")
async def approve_action(
    approval_id: str,
    token: str = Query(..., description="HMAC-SHA256 signed approval token"),
    approved_by: str = Query("operator", description="Identifier for approving operator"),
) -> dict:
    """Approve a pending high-impact action.

    Verifies the HMAC-signed token and marks the action as approved.
    The action will execute immediately after approval.
    """
    from app.core.approval import approve
    try:
        result = approve(approval_id, token, approved_by=approved_by)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    logger.info({
        "message": "approval_endpoint_approved",
        "approval_id": approval_id,
        "approved_by": approved_by,
    })
    return result


@router.post("/approvals/{approval_id}/reject")
async def reject_action(
    approval_id: str,
    token: str = Query(..., description="HMAC-SHA256 signed approval token"),
    rejected_by: str = Query("operator", description="Identifier for rejecting operator"),
) -> dict:
    """Reject a pending high-impact action.

    Verifies the HMAC-signed token and cancels the proposed action.
    """
    from app.core.approval import reject
    try:
        result = reject(approval_id, token, rejected_by=rejected_by)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    logger.info({
        "message": "approval_endpoint_rejected",
        "approval_id": approval_id,
        "rejected_by": rejected_by,
    })
    return result

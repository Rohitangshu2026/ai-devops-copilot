from fastapi import APIRouter, HTTPException

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
async def unfreeze_service(service: str) -> dict:
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
async def admin_reload_policy() -> dict:
    """Hot-reload policy.yaml from disk.  SIGHUP also triggers this."""
    policy = reload_policy()
    return {
        "actions": list(policy.actions.keys()),
        "decision_rows": len(policy.decision_table),
        "global": policy.global_.model_dump(),
    }

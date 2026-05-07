from __future__ import annotations

import time
import uuid
from dataclasses import asdict

from app.core.anomaly import compute_anomaly_score
from app.core.audit import record_analysis
from app.core.causality import DEPENDENCY_MAP, validate_causality
from app.core.confidence import score_confidence
from app.core.action_executor import execute_async as action_execute
from app.core.impact import schedule_verification
from app.core.safety import validate as safety_validate
from app.log_processor.classifier import classify_severity
from app.log_processor.extractor import extract_relevant
from app.log_processor.parser import detect_error_type, extract_key_events
from app.log_processor.summarizer import summarize
from app.llm.client import analyze
from app.models.schemas import AnalysisRequest, AnalysisResult, ParsedLog
from app.services.elk_service import fetch_logs
from app.services.memory_store import find_recent_incidents_for_chain, link_incident_to_chain
from app.utils.logger import get_logger

logger = get_logger("agent")


async def run_analysis(req: AnalysisRequest) -> AnalysisResult:
    _t0 = time.monotonic()
    logger.info({"message": "analysis_started", "service": req.service, "env": req.environment})

    raw_logs = await fetch_logs(req.service, req.environment.value, req.lookback_minutes)
    if not raw_logs:
        raise ValueError(
            f"No logs found for service='{req.service}' in the last {req.lookback_minutes}m. "
            "Run simulate_failure.sh to generate log data."
        )

    relevant = extract_relevant(raw_logs)
    # Pass raw_logs so the timeline includes all events (health checks provide
    # time context for error_rate computation). relevant is used for error analysis.
    summary = summarize(raw_logs)

    error_type = detect_error_type(relevant)
    key_events = extract_key_events(relevant)
    severity = classify_severity(relevant, error_type)
    confidence_hint, confidence_score, confidence_breakdown = await score_confidence(
        summary, error_type, severity, service=req.service
    )

    raw_evidence = [str(l.get("message", l)) for l in relevant]

    llm_result = await analyze(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        key_events=key_events,
        summary=summary,
        lookback_minutes=req.lookback_minutes,
    )

    # ranked hypotheses — LLM returns root_causes array
    root_causes: list = llm_result.get("root_causes") or []
    if not root_causes:
        # fallback: wrap legacy root_cause string
        root_causes = [{"cause": llm_result.get("root_cause", ""), "confidence": 0.5}]
    primary_cause = root_causes[0].get("cause", "")

    # causality validation against log evidence
    causality = validate_causality(summary, primary_cause, req.service)
    action_target = causality.action_target or req.service

    if causality.target_redirected:
        logger.info({
            "message": "causality_target_redirected",
            "service": req.service,
            "redirected_to": action_target,
        })

    # ── Phase 9e — Compute statistical anomaly score ─────────────────────────
    # Default -1.0 means "no baseline available" → anomaly gate is skipped.
    # A real z-score (>= 0.0) from an established baseline gates destructive actions.
    _anomaly_score: float = -1.0
    try:
        _anomaly_score = await compute_anomaly_score(req.service, summary)
    except Exception:  # noqa: BLE001
        pass  # gate bypassed when score cannot be computed

    # ── Phase 8e — Pre-safety: determine cascade depth ───────────────────────
    _pre_cascade_depth: int = 0
    _pre_upstream_match: dict | None = None
    try:
        from datetime import datetime, timezone as _tz
        _now_iso = datetime.now(_tz.utc).isoformat()
        _recent = await find_recent_incidents_for_chain(_now_iso, lookback_minutes=10)
        _upstream_services = set(DEPENDENCY_MAP.get(req.service, []))
        for _inc in _recent:
            if _inc.get("service") in _upstream_services:
                _pre_upstream_match = _inc
                _pre_cascade_depth = (_inc.get("cascade_depth") or 0) + 1
                break
    except Exception:  # noqa: BLE001
        pass

    # ── Phase 5 + 9: safety stack ─────────────────────────────────────────────
    safety = await safety_validate(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        confidence=confidence_hint,
        proposed_action=llm_result.get("proposed_action", {}),
        causality=causality,
        cascade_depth=_pre_cascade_depth,
        anomaly_score=_anomaly_score,
    )

    # Override proposed action if safety denied or modified it
    final_action = {**llm_result.get("proposed_action", {}), "type": safety.action}

    # Execute if action is not notify/no_action.
    # execute_async returns immediately with action_state="executing" so the
    # HTTP response is not held open during the rollout polling loop.
    action_id = str(uuid.uuid4())
    execution_result = None
    if safety.action not in ("notify", "no_action"):
        execution_result = await action_execute(
            action_id=action_id,
            action_type=safety.action,
            service=action_target,
            dry_run=(req.environment.value == "dev"),
        )
        # Schedule impact verification 2 min later (persisted in ES, survives restart)
        await schedule_verification(
            incident_id=action_id,
            service=req.service,
            environment=req.environment.value,
            baseline_error_ratio=summary.error_ratio,
        )

    # Audit log
    incident_id = await record_analysis(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        confidence_hint=confidence_hint,
        confidence_score=confidence_score,
        causality_verified=causality.verified,
        causality_evidence=causality.matched_evidence,
        root_causes=root_causes,
        proposed_action=final_action,
        safety_decision="allowed" if safety.allowed else "denied",
        safety_reason=safety.reason,
        log_summary=asdict(summary),
        tool_calls=llm_result.pop("_tool_calls", []),
    )

    # ── Phase 8e — Temporal incident correlation (link after audit) ──────────
    chain_id: str | None = None
    upstream_incident_id: str | None = None
    cascade_depth: int = 0
    cascade_path: list[str] = []

    try:
        if _pre_upstream_match:
            upstream_id = _pre_upstream_match.get("incident_id", "")
            chain_id = _pre_upstream_match.get("incident_chain_id") or str(uuid.uuid4())
            upstream_incident_id = upstream_id
            cascade_depth = _pre_cascade_depth
            cascade_path = list(_pre_upstream_match.get("cascade_path") or []) + [req.service]
            await link_incident_to_chain(incident_id, chain_id, upstream_id, cascade_depth, cascade_path)
            logger.info({
                "message": "incident_chain_linked",
                "incident_id": incident_id,
                "chain_id": chain_id,
                "upstream_id": upstream_id,
                "cascade_depth": cascade_depth,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "temporal_correlation_failed", "error": str(exc)})

    result = AnalysisResult(
        service=req.service,
        environment=req.environment.value,
        root_cause=primary_cause,
        root_causes=root_causes,
        suggestion=llm_result.get("suggestion", ""),
        confidence_hint=confidence_hint,
        confidence_score=confidence_score,
        confidence_source="signal",
        confidence_breakdown=confidence_breakdown,
        parsed_log=ParsedLog(
            error_type=error_type,
            severity=severity,
            key_events=key_events,
            summary=primary_cause,
        ),
        raw_evidence=raw_evidence[:20],
        log_summary=asdict(summary),
        proposed_action=final_action,
        causality_verified=causality.verified,
        causality_target=action_target if causality.target_redirected else None,
        safety_decision="allowed" if safety.allowed else "denied",
        safety_reason=safety.reason,
        safety_checks=safety.checks,
        incident_id=incident_id,
        execution_result=asdict(execution_result) if execution_result is not None else None,
        incident_chain_id=chain_id,
        upstream_incident_id=upstream_incident_id,
        cascade_depth=cascade_depth,
        cascade_path=cascade_path,
        anomaly_score=_anomaly_score,
        cross_validation=llm_result.pop("_cross_validation", None),
    )

    logger.info({
        "message": "analysis_complete",
        "service": req.service,
        "confidence": confidence_hint,
        "score": confidence_score,
        "error_type": error_type,
        "causality_verified": causality.verified,
        "root_causes_count": len(root_causes),
        "change_point": summary.change_point_description,
        "safety_decision": safety.action,
        "incident_id": incident_id,
    })

    # ── Phase 8c — Prometheus metrics ────────────────────────────────────────
    try:
        from app.utils.prom_metrics import analysis_duration, analysis_total
        outcome_label = result.execution_result.get("status", "unknown") if result.execution_result else "no_action"
        analysis_total.labels(service=req.service, outcome=outcome_label).inc()
        analysis_duration.observe(time.monotonic() - _t0)
    except Exception:  # noqa: BLE001
        pass

    return result

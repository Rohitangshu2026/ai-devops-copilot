from dataclasses import asdict

from app.core.causality import validate_causality
from app.core.confidence import score_confidence
from app.log_processor.classifier import classify_severity
from app.log_processor.extractor import extract_relevant
from app.log_processor.parser import detect_error_type, extract_key_events
from app.log_processor.summarizer import summarize
from app.llm.client import analyze
from app.models.schemas import AnalysisRequest, AnalysisResult, ParsedLog
from app.services.elk_service import fetch_logs
from app.utils.logger import get_logger

logger = get_logger("agent")


async def run_analysis(req: AnalysisRequest) -> AnalysisResult:
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
    confidence_hint, confidence_score = score_confidence(summary, error_type, severity)

    raw_evidence = [str(l.get("message", l)) for l in relevant]

    llm_result = await analyze(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        key_events=key_events,
        summary=summary,
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

    result = AnalysisResult(
        service=req.service,
        environment=req.environment.value,
        root_cause=primary_cause,
        root_causes=root_causes,
        suggestion=llm_result.get("suggestion", ""),
        confidence_hint=confidence_hint,
        confidence_score=confidence_score,
        confidence_source="signal",
        parsed_log=ParsedLog(
            error_type=error_type,
            severity=severity,
            key_events=key_events,
            summary=primary_cause,
        ),
        raw_evidence=raw_evidence[:20],
        log_summary=asdict(summary),
        proposed_action=llm_result.get("proposed_action"),
        causality_verified=causality.verified,
        causality_target=action_target if causality.target_redirected else None,
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
    })
    return result

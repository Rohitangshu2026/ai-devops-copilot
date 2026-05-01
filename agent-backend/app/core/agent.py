from app.log_processor.classifier import classify_severity
from app.log_processor.extractor import extract_relevant
from app.log_processor.parser import detect_error_type, extract_key_events
from app.llm.client import analyze
from app.models.schemas import AnalysisRequest, AnalysisResult, ParsedLog
from app.services.elk_service import fetch_logs
from app.utils.logger import get_logger

logger = get_logger("agent")


async def run_analysis(req: AnalysisRequest) -> AnalysisResult:
    logger.info({"message": "analysis_started", "service": req.service, "env": req.environment})

    raw_logs = await fetch_logs(req.service, req.environment.value, req.lookback_minutes)
    if not raw_logs:
        raise ValueError(f"No logs found for service='{req.service}' in the last {req.lookback_minutes}m. "
                         "Run simulate_failure.sh to generate log data.")
    relevant = extract_relevant(raw_logs)

    error_type = detect_error_type(relevant)
    key_events = extract_key_events(relevant)
    severity = classify_severity(relevant, error_type)
    raw_evidence = [str(l.get("message", l)) for l in relevant]

    llm_result = await analyze(
        service=req.service,
        environment=req.environment.value,
        error_type=error_type,
        severity=severity,
        key_events=key_events,
        raw_evidence=raw_evidence,
    )

    result = AnalysisResult(
        service=req.service,
        environment=req.environment.value,
        root_cause=llm_result["root_cause"],
        suggestion=llm_result["suggestion"],
        confidence_hint=llm_result["confidence_hint"],
        parsed_log=ParsedLog(
            error_type=error_type,
            severity=severity,
            key_events=key_events,
            summary=llm_result["root_cause"],
        ),
        raw_evidence=raw_evidence[:20],
    )

    logger.info({"message": "analysis_complete", "service": req.service, "confidence": result.confidence_hint})
    return result

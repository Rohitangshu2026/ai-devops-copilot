from fastapi import APIRouter, HTTPException

from app.core.agent import run_analysis
from app.models.schemas import AnalysisRequest, AnalysisResult
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

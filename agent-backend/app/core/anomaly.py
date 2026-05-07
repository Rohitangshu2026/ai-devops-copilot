"""Statistical anomaly detection baseline (Phase 9e).

Maintains a rolling 7-day baseline of error_ratio per service in the
``devops-baselines`` ES index.  On each analysis run, ``compute_anomaly_score``
returns the z-score of the current window's error_ratio against that baseline.

Safety gate integration: a z-score below ``ANOMALY_Z_THRESHOLD`` means the
current error rate is not statistically anomalous — the system should not
execute destructive actions on routine traffic fluctuations.

The baseline is updated by the background sweeper in main.py (daily cadence).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.log_processor.summarizer import LogSummary
from app.utils.logger import get_logger

logger = get_logger("anomaly")

_BASELINES_INDEX = "devops-baselines"
# z-score must exceed this for a destructive action to be considered safe.
ANOMALY_Z_THRESHOLD = 2.0
# Minimum samples needed before the baseline is trusted.
_MIN_SAMPLES = 5


async def compute_anomaly_score(service: str, summary: LogSummary) -> float:
    """Return the z-score of *summary.error_ratio* vs the stored baseline.

    Returns 0.0 when:
    - Baseline is missing or has fewer than MIN_SAMPLES data points.
    - Standard deviation is near-zero (baseline is perfectly stable).

    A positive z-score means the current error rate is above the historical
    mean.  A z > ANOMALY_Z_THRESHOLD indicates a genuine anomaly.
    """
    baseline = await get_baseline(service)
    if not baseline:
        logger.debug({"message": "anomaly_no_baseline", "service": service})
        return 0.0

    sample_count = baseline.get("sample_count", 0)
    if sample_count < _MIN_SAMPLES:
        logger.debug({
            "message": "anomaly_insufficient_samples",
            "service": service,
            "samples": sample_count,
        })
        return 0.0

    mean = float(baseline.get("mean_error_ratio", 0.0))
    std = float(baseline.get("std_error_ratio", 0.0))

    if std < 1e-6:
        # Perfectly stable baseline — any error at all is infinitely anomalous;
        # cap at a high but finite value so callers can apply thresholds.
        if summary.error_ratio > 0:
            return 10.0
        return 0.0

    z = (summary.error_ratio - mean) / std
    logger.debug({
        "message": "anomaly_score_computed",
        "service": service,
        "z_score": round(z, 3),
        "error_ratio": summary.error_ratio,
        "baseline_mean": mean,
        "baseline_std": std,
    })
    return z


async def get_baseline(service: str) -> dict[str, Any] | None:
    """Fetch the stored baseline document for *service* from ES."""
    try:
        from app.services.elk_service import get_client
        client = get_client()
        resp = await client.get(index=_BASELINES_INDEX, id=service)
        return resp.get("_source") if resp.get("found") else None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "not_found" in msg or "404" in msg:
            return None
        logger.warning({"message": "get_baseline_failed", "service": service, "error": str(exc)})
        return None


async def update_baseline(service: str, error_ratios: list[float]) -> None:
    """Compute and persist a new baseline from a list of recent error_ratio samples.

    Called by the daily background sweeper.  The document is upserted so a
    missing baseline is created on first run.

    Args:
        service: The service name (used as the ES document id).
        error_ratios: List of error_ratio floats from recent analysis windows.
    """
    if not error_ratios:
        return

    n = len(error_ratios)
    mean = sum(error_ratios) / n
    variance = sum((x - mean) ** 2 for x in error_ratios) / n
    std = math.sqrt(variance)

    doc: dict[str, Any] = {
        "service": service,
        "mean_error_ratio": round(mean, 6),
        "std_error_ratio": round(std, 6),
        "sample_count": n,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        from app.services.elk_service import get_client
        client = get_client()
        await client.index(
            index=_BASELINES_INDEX,
            id=service,
            document=doc,
            refresh="wait_for",
        )
        logger.info({
            "message": "baseline_updated",
            "service": service,
            "mean": doc["mean_error_ratio"],
            "std": doc["std_error_ratio"],
            "samples": n,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "update_baseline_failed", "service": service, "error": str(exc)})


async def refresh_baseline_for_service(service: str, lookback_days: int = 7) -> None:
    """Query recent incidents for *service* and recompute its baseline.

    Called by the background sweeper.  Silently no-ops if ES is unavailable.
    """
    try:
        from app.services.elk_service import get_client
        client = get_client()
        resp = await client.search(
            index="devops-incidents-*",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"service.keyword": service}},
                            {"range": {"timestamp": {"gte": f"now-{lookback_days}d"}}},
                        ]
                    }
                },
                "_source": ["log_summary.error_ratio"],
                "size": 500,
            },
        )
        ratios = []
        for hit in resp.get("hits", {}).get("hits", []):
            ratio = (
                hit.get("_source", {})
                   .get("log_summary", {})
                   .get("error_ratio")
            )
            if ratio is not None:
                ratios.append(float(ratio))

        if ratios:
            await update_baseline(service, ratios)
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "message": "refresh_baseline_failed",
            "service": service,
            "error": str(exc),
        })

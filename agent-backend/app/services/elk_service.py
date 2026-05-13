from typing import List

from elasticsearch import AsyncElasticsearch

from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("elk_service")

_client: AsyncElasticsearch | None = None


def get_client() -> AsyncElasticsearch:
    global _client
    if _client is None:
        _client = AsyncElasticsearch(settings.es_url)
    return _client


def _service_clause(service: str) -> dict:
    """Match `service` regardless of whether ES stored it as a string or an
    array (Logstash + k8s Filebeat can create ["sample-app","sample-app"]).
    A nested bool/should with both the analyzed and keyword sub-field covers
    all dynamic-mapping variants."""
    return {
        "bool": {
            "should": [
                {"term":  {"service.keyword": service}},
                {"match": {"service": service}},
            ],
            "minimum_should_match": 1,
        }
    }


def _environment_clause(environment: str) -> dict:
    return {
        "bool": {
            "should": [
                {"term":  {"environment.keyword": environment}},
                {"match": {"environment": environment}},
            ],
            "minimum_should_match": 1,
        }
    }


async def fetch_logs(service: str, environment: str, lookback_minutes: int) -> List[dict]:
    client = get_client()
    must_clauses: list = [{"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}}]
    if service:
        must_clauses.append(_service_clause(service))
    if environment:
        must_clauses.append(_environment_clause(environment))

    query = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 500,
    }
    resp = await client.search(index=settings.es_index, body=query)
    hits = resp["hits"]["hits"]
    docs = [h["_source"] for h in hits]

    error_hits = sum(1 for d in docs if str(d.get("level", "")).upper() in ("ERROR", "CRITICAL"))
    logger.info({
        "message": "es_query_complete",
        "service": service,
        "environment": environment,
        "lookback_minutes": lookback_minutes,
        "total_hits": len(docs),
        "error_hits": error_hits,
    })

    # ── Stale-data diagnostic ────────────────────────────────────────────────
    # When the window is empty, check whether ANY data exists for this service
    # so we can surface a helpful message instead of a generic 404.
    if not docs:
        await _log_stale_data_hint(client, service, environment)

    return docs


async def _log_stale_data_hint(
    client: AsyncElasticsearch,
    service: str,
    environment: str,
) -> None:
    """Fire a single no-time-filter query to detect stale data and log a hint."""
    try:
        clauses: list = []
        if service:
            clauses.append(_service_clause(service))
        if environment:
            clauses.append(_environment_clause(environment))
        q: dict = (
            {"query": {"bool": {"must": clauses}}} if clauses
            else {"query": {"match_all": {}}}
        )
        q["sort"] = [{"@timestamp": {"order": "desc"}}]
        q["size"] = 1
        r = await client.search(index=settings.es_index, body=q)
        stale_hits = r["hits"]["hits"]
        if stale_hits:
            most_recent_ts = stale_hits[0]["_source"].get("@timestamp", "unknown")
            logger.warning({
                "message": "es_stale_data_detected",
                "hint": (
                    f"Data exists for service='{service}' env='{environment}' "
                    f"but most-recent log is {most_recent_ts}. "
                    "Run simulate_failure.sh to generate fresh events, "
                    "then re-analyze within the lookback window."
                ),
                "most_recent_timestamp": most_recent_ts,
            })
        else:
            logger.warning({
                "message": "es_no_data_at_all",
                "service": service,
                "environment": environment,
                "hint": (
                    "No documents found at all for this service/environment. "
                    "Check Filebeat and Logstash are running and the service "
                    "is emitting JSON logs."
                ),
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug({"message": "stale_data_hint_failed", "error": str(exc)})


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None

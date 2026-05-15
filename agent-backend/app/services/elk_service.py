from typing import List, Optional

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


def _resolve_index(index_pattern: Optional[str], service: Optional[str]) -> str:
    """Resolve which ES index pattern to query.

    Precedence (highest first):
      1. Explicit ``index_pattern`` argument (caller knows best)
      2. Owning platform's ``log_index_pattern`` (looked up via the registry)
      3. Global ``settings.es_index`` fallback

    The lookup is wrapped in ``try/except`` so any import or registry failure
    falls back to the global default — the refactor must never regress the
    pre-refactor single-platform path.
    """
    if index_pattern:
        return index_pattern
    if service:
        try:
            from app.platforms.registry import get_registry
            plat = get_registry().for_service(service)
            if plat is not None and plat.log_index_pattern:
                return plat.log_index_pattern
        except Exception:  # noqa: BLE001
            pass
    return settings.es_index


def _service_clause(service: str, field: str = "service") -> dict:
    """Match `service` regardless of whether ES stored it as a string or an
    array (Logstash + k8s Filebeat can create ["sample-app","sample-app"]).
    A nested bool/should with both the analyzed and keyword sub-field covers
    all dynamic-mapping variants.

    The ``field`` argument lets a platform override the ES field name (for
    fleets that ship logs under ``app`` or ``service.name`` instead of the
    flat ``service`` field).
    """
    return {
        "bool": {
            "should": [
                {"term":  {f"{field}.keyword": service}},
                {"match": {field: service}},
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


async def fetch_logs(
    service: str,
    environment: str,
    lookback_minutes: int,
    *,
    index_pattern: Optional[str] = None,
    service_field: str = "service",
    namespace: Optional[str] = None,
    pod_name: Optional[str] = None,
) -> List[dict]:
    """Fetch recent logs for *service* / *environment* from Elasticsearch.

    New keyword-only arguments (multi-platform refactor):
      * ``index_pattern`` — override the index pattern resolved from the
        platform registry; falls back to ``settings.es_index``.
      * ``service_field`` — ES field that holds the service name (default
        ``service``).  Allows a platform to ship logs under a different field.
      * ``namespace`` — when set, adds a ``kubernetes.namespace.keyword`` term
        filter to scope results to a specific k8s namespace.
      * ``pod_name`` — when set, adds a ``kubernetes.pod.name.keyword`` term
        filter to scope results to a specific pod.

    Existing positional callers continue to work unchanged.
    """
    client = get_client()
    resolved_index = _resolve_index(index_pattern, service)
    must_clauses: list = [{"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}}]
    if service:
        must_clauses.append(_service_clause(service, field=service_field))
    # SpyRoom logs currently do not emit an `environment` field.
    # Skip env filtering for local/dev platforms until structured logging is enabled.
    if environment and environment not in ("dev", ""):
        must_clauses.append(_environment_clause(environment))
    if namespace:
        must_clauses.append({"term": {"kubernetes.namespace.keyword": namespace}})
    if pod_name:
        must_clauses.append({"term": {"kubernetes.pod.name.keyword": pod_name}})

    query = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 500,
    }

    logger.info({
        "message": "es_debug_query",
        "query": query,
        "index": resolved_index,
    })
    resp = await client.search(index=resolved_index, body=query)
    hits = resp["hits"]["hits"]
    docs = [h["_source"] for h in hits]

    error_hits = sum(1 for d in docs if str(d.get("level", "")).upper() in ("ERROR", "CRITICAL"))
    logger.info({
        "message": "es_query_complete",
        "service": service,
        "environment": environment,
        "lookback_minutes": lookback_minutes,
        "index": resolved_index,
        "total_hits": len(docs),
        "error_hits": error_hits,
    })

    # ── Stale-data diagnostic ────────────────────────────────────────────────
    # When the window is empty, check whether ANY data exists for this service
    # so we can surface a helpful message instead of a generic 404.
    if not docs:
        await _log_stale_data_hint(
            client, service, environment,
            index_pattern=resolved_index,
            service_field=service_field,
        )

    return docs


async def _log_stale_data_hint(
    client: AsyncElasticsearch,
    service: str,
    environment: str,
    *,
    index_pattern: Optional[str] = None,
    service_field: str = "service",
) -> None:
    """Fire a single no-time-filter query to detect stale data and log a hint."""
    try:
        clauses: list = []
        if service:
            clauses.append(_service_clause(service, field=service_field))
        if environment and environment not in ("dev", ""):
            clauses.append(_environment_clause(environment))
        q: dict = (
            {"query": {"bool": {"must": clauses}}} if clauses
            else {"query": {"match_all": {}}}
        )
        q["sort"] = [{"@timestamp": {"order": "desc"}}]
        q["size"] = 1
        r = await client.search(index=index_pattern or settings.es_index, body=q)
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

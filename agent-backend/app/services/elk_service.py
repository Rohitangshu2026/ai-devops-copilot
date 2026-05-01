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


async def fetch_logs(service: str, environment: str, lookback_minutes: int) -> List[dict]:
    client = get_client()
    should_boosts = []
    if service:
        should_boosts = [
            {"term": {"service.keyword": service}},
            {"term": {"service": service}},
        ]
    query = {
        "query": {
            "bool": {
                "must": [{"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}}],
                "should": should_boosts,
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 200,
    }
    resp = await client.search(index=settings.es_index, body=query)
    hits = resp["hits"]["hits"]
    logger.info({"message": "es_query_complete", "service": service, "hits": len(hits)})
    return [h["_source"] for h in hits]


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None

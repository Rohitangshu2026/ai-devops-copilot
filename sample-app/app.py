"""Sample app — monitored service for the AI DevOps Copilot.

Phase 7 additions: env-var-controlled failure mode endpoints used by e2e tests
and the eval fixture generator.  All new endpoints are disabled by default
(env vars not set) so the existing docker-compose behaviour is unchanged.
"""
import asyncio
import os

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from logger import get_logger

app = FastAPI()
logger = get_logger()
_ENV = os.getenv("ENVIRONMENT", "dev")


@app.get("/")
def root():
    logger.info({"event": "root_hit", "endpoint": "/", "status": 200, "environment": _ENV})
    return {"message": "Version 2 deployed 🚀"}


@app.get("/health")
def health():
    logger.info({"event": "health_check", "endpoint": "/health", "status": 200, "environment": _ENV})
    return {"status": "ok"}


@app.get("/error")
def error():
    try:
        raise Exception("Simulated failure for testing")
    except Exception as e:
        logger.error({"event": "error", "endpoint": "/error", "error": str(e), "status": 500, "environment": _ENV})
        return {"status": "error", "message": str(e)}


# ── Phase 7 / Phase 9c failure-mode endpoints ─────────────────────────────────


@app.get("/slow")
async def slow():
    """Simulate a slow upstream dependency.

    SLOW_MS (int): milliseconds to sleep before responding.
    SLOW_THRESHOLD_MS (int): if delay > threshold, return 504 Gateway Timeout.
    """
    delay_ms = int(os.getenv("SLOW_MS", "0"))
    threshold_ms = int(os.getenv("SLOW_THRESHOLD_MS", "2000"))

    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)

    if delay_ms > threshold_ms:
        logger.warning({
            "event": "slow_response", "endpoint": "/slow",
            "delay_ms": delay_ms, "status": 504, "environment": _ENV,
        })
        return JSONResponse(
            status_code=504,
            content={"status": "timeout", "delay_ms": delay_ms},
        )

    logger.info({
        "event": "slow_response", "endpoint": "/slow",
        "delay_ms": delay_ms, "status": 200, "environment": _ENV,
    })
    return {"status": "ok", "delay_ms": delay_ms}


@app.get("/oom")
def oom():
    """Simulate memory pressure / OOM allocation.

    MEM_MB (int): megabytes to allocate; endpoint is disabled (200 ok) when 0.
    The allocation is intentional — it allows k8s OOMKill events to be generated.
    """
    mem_mb = int(os.getenv("MEM_MB", "0"))

    if mem_mb <= 0:
        logger.info({"event": "oom_disabled", "endpoint": "/oom", "status": 200, "environment": _ENV})
        return {"status": "disabled", "message": "Set MEM_MB env var to enable"}

    # Allocate mem_mb megabytes.  This is intentional for OOM testing.
    chunks = []
    for _ in range(mem_mb):
        chunks.append(b"x" * (1024 * 1024))

    logger.warning({
        "event": "oom_allocated", "endpoint": "/oom",
        "mem_mb": mem_mb, "status": 200, "environment": _ENV,
    })
    return {"status": "allocated", "mem_mb": mem_mb, "bytes": mem_mb * 1024 * 1024}


@app.get("/crash")
def crash():
    """Simulate an unhandled exception (CrashLoopBackOff trigger).

    CRASH_MESSAGE (str): custom exception message written to logs.
    """
    message = os.getenv("CRASH_MESSAGE", "Intentional crash for testing")
    logger.error({
        "event": "crash", "endpoint": "/crash",
        "error": message, "status": 500, "environment": _ENV,
    })
    raise RuntimeError(message)


@app.get("/dep-error")
async def dep_error():
    """Simulate a dependency connection failure.

    DOWNSTREAM_URL (str): URL to attempt to reach.  Returns 503 on failure.
    Endpoint is disabled (200 ok) when DOWNSTREAM_URL is not set.
    """
    downstream_url = os.getenv("DOWNSTREAM_URL", "")

    if not downstream_url:
        logger.info({"event": "dep_error_disabled", "endpoint": "/dep-error", "status": 200, "environment": _ENV})
        return {"status": "disabled", "message": "Set DOWNSTREAM_URL env var to enable"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(downstream_url)
        logger.info({
            "event": "dep_ok", "endpoint": "/dep-error",
            "downstream": downstream_url, "upstream_status": resp.status_code,
            "status": 200, "environment": _ENV,
        })
        return {"status": "ok", "upstream_status": resp.status_code}
    except Exception as exc:
        logger.error({
            "event": "dep_error", "endpoint": "/dep-error",
            "error": str(exc), "downstream": downstream_url,
            "status": 503, "environment": _ENV,
        })
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": str(exc)},
        )

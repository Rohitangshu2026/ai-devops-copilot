"""Agent-backend entry point.

Lifecycle (Phase 6 hardening):
1. Bootstrap ILM policy on the incident index (Phase 6c).
2. Recover orphaned execution leases left by previous pod crashes (Phase 6h).
3. Launch the persistent verification sweeper as a background task (Phase 6b).
4. Register a SIGHUP handler to hot-reload policy.yaml (Phase 6i).
"""
from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.impact import run_verification_sweeper
from app.core.policy import load_policy, reload_policy
from app.services.elk_service import close_client
from app.services.memory_store import bootstrap_ilm_policy, recover_orphaned_leases
from app.utils.logger import get_logger

logger = get_logger("main")

_sweeper_task: asyncio.Task | None = None
_sweeper_stop: asyncio.Event | None = None


def _install_sighup_handler() -> None:
    """Install a SIGHUP handler that hot-reloads policy.yaml.

    SIGHUP is unsupported on Windows; the call is a no-op there.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _on_sighup() -> None:
        logger.info({"message": "sighup_received"})
        reload_policy()

    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, AttributeError, ValueError, RuntimeError):
        # Windows: signal.SIGHUP missing or add_signal_handler unsupported.
        # RuntimeError: not running in main thread (e.g. TestClient).
        logger.info({"message": "sighup_handler_unsupported"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sweeper_task, _sweeper_stop
    logger.info({"message": "agent_backend_starting"})

    # 1. Load policy at startup; refuse to start if invalid.
    try:
        policy = load_policy()
        logger.info({"message": "policy_loaded", "actions": list(policy.actions.keys())})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "policy_load_failed", "error": str(exc)})

    # 2. Bootstrap ILM policy (best-effort).
    try:
        await bootstrap_ilm_policy()
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "ilm_bootstrap_failed_at_startup", "error": str(exc)})

    # 3. Recover orphaned leases from previous pod crashes.
    try:
        recovered = await recover_orphaned_leases(older_than_seconds=90)
        if recovered:
            logger.info({"message": "leases_recovered_at_startup", "count": len(recovered)})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "lease_recovery_failed_at_startup", "error": str(exc)})

    # 4. Launch verification sweeper.
    _sweeper_stop = asyncio.Event()
    _sweeper_task = asyncio.create_task(run_verification_sweeper(_sweeper_stop))

    # 5. SIGHUP → reload policy.
    _install_sighup_handler()

    yield

    logger.info({"message": "agent_backend_stopping"})
    if _sweeper_stop is not None:
        _sweeper_stop.set()
    if _sweeper_task is not None and not _sweeper_task.done():
        try:
            await asyncio.wait_for(_sweeper_task, timeout=5.0)
        except asyncio.TimeoutError:
            _sweeper_task.cancel()
    await close_client()
    logger.info({"message": "agent_backend_stopped"})


app = FastAPI(title="AI DevOps Copilot", version="0.6.0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}

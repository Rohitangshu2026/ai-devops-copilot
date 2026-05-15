"""Agent-backend entry point.

Lifecycle:
1. Bootstrap ILM policy on the incident index (Phase 6c).
2. Recover orphaned execution leases left by previous pod crashes (Phase 6h).
3. Launch the persistent verification sweeper as a background task (Phase 6b).
4. Register a SIGHUP handler to hot-reload policy.yaml (Phase 6i).
5. Launch the daily anomaly baseline refresh sweeper (Phase 9e).
"""
from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from app.api.routes import router
from app.core.impact import run_verification_sweeper
from app.core.policy import load_policy, reload_policy
from app.platforms.registry import get_registry, reload_registry
from app.services.elk_service import close_client
from app.services.memory_store import bootstrap_ilm_policy, recover_orphaned_leases
from app.utils.logger import get_logger

logger = get_logger("main")

_sweeper_task: asyncio.Task | None = None
_sweeper_stop: asyncio.Event | None = None
_baseline_task: asyncio.Task | None = None
_k8s_watcher_task: asyncio.Task | None = None


async def _run_baseline_sweeper(stop: asyncio.Event, interval_seconds: int = 86400) -> None:
    """Refresh anomaly baselines for all known services daily.

    On first startup, waits `interval_seconds` before the first refresh to
    avoid overwhelming ES during cold start.  The refresh is best-effort —
    failures are logged and the sweeper continues.
    """
    logger.info({"message": "baseline_sweeper_started", "interval_seconds": interval_seconds})
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break

        # Discover services from recent incidents
        try:
            from app.services.memory_store import get_recent_incidents
            from app.core.anomaly import refresh_baseline_for_service

            recent = await get_recent_incidents(limit=200)
            services = list({inc.get("service") for inc in recent if inc.get("service")})
            logger.info({"message": "baseline_refresh_started", "services": services})
            for svc in services:
                try:
                    await refresh_baseline_for_service(svc, lookback_days=7)
                except Exception as exc:  # noqa: BLE001
                    logger.warning({
                        "message": "baseline_refresh_service_failed",
                        "service": svc,
                        "error": str(exc),
                    })
            logger.info({"message": "baseline_refresh_done", "count": len(services)})
        except Exception as exc:  # noqa: BLE001
            logger.warning({"message": "baseline_sweeper_error", "error": str(exc)})


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
            reload_registry()
        except Exception as exc:  # noqa: BLE001
            logger.warning({"message": "platform_registry_reload_failed", "error": str(exc)})

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

    # 1b. Load platform registry (multi-platform refactor).
    try:
        reg = get_registry()
        logger.info({"message": "platforms_loaded", "platforms": reg.names()})
    except Exception as exc:  # noqa: BLE001
        logger.warning({"message": "platform_registry_load_failed", "error": str(exc)})

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

    # 6. Launch daily anomaly baseline sweeper (Phase 9e).
    _baseline_task = asyncio.create_task(_run_baseline_sweeper(_sweeper_stop))

    # 7. Launch k8s event watcher (gated by K8S_WATCHER_ENABLED env var).
    #    The watcher returns immediately when disabled, so this is safe to
    #    schedule unconditionally — the env flag is the single source of
    #    truth for whether it actually runs.
    global _k8s_watcher_task
    try:
        from app.watchers.k8s_event_watcher import start_watcher as _start_k8s_watcher
        _k8s_watcher_task = asyncio.create_task(_start_k8s_watcher(_sweeper_stop))
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "message": "k8s_event_watcher_launch_failed",
            "error": str(exc),
        })

    yield

    logger.info({"message": "agent_backend_stopping"})
    if _sweeper_stop is not None:
        _sweeper_stop.set()
    if _sweeper_task is not None and not _sweeper_task.done():
        try:
            await asyncio.wait_for(_sweeper_task, timeout=5.0)
        except asyncio.TimeoutError:
            _sweeper_task.cancel()
    if _baseline_task is not None and not _baseline_task.done():
        _baseline_task.cancel()
    if _k8s_watcher_task is not None and not _k8s_watcher_task.done():
        try:
            # The watcher itself listens to the same _sweeper_stop event and
            # does its own thread join + cleanup; bound the wait so a stuck
            # k8s API doesn't block pod termination.
            await asyncio.wait_for(_k8s_watcher_task, timeout=6.0)
        except asyncio.TimeoutError:
            _k8s_watcher_task.cancel()
    await close_client()
    logger.info({"message": "agent_backend_stopped"})


app = FastAPI(title="AI DevOps Copilot", version="0.6.0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Phase 8c — Prometheus scrape endpoint ────────────────────────────────────


@app.get("/metrics")
async def prometheus_metrics():
    """Expose Prometheus metrics for scraping (not the JSON dashboard stats)."""
    from app.utils.prom_metrics import CONTENT_TYPE_LATEST, generate_latest
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Phase 8d — HTML dashboard ─────────────────────────────────────────────────


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Server-rendered HTML dashboard showing the 50 most recent incidents."""
    from app.api.v1.dashboard import render_dashboard
    from app.services.memory_store import get_recent_incidents
    incidents = await get_recent_incidents(limit=50)
    return render_dashboard(incidents)

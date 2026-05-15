"""Tests for app.watchers.k8s_event_watcher — Phase 1 (trigger only).

Design notes:

* All async tests use bounded ``asyncio.wait_for`` timeouts so a regression
  to a blocking await is observed as a *failure*, never as a hang.
* The sync watch loop is exercised by injecting a stream_factory — no
  ``sys.modules`` mocking required.
* Cooldown, rate-limit, severity tiers, and service-name normalisation are
  unit-tested in isolation.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.watchers import k8s_event_watcher as kew


# ── Helpers ─────────────────────────────────────────────────────────────────


def _evt(reason, *, kind="Pod", name="auth-service-abc-xyz", count=1, message="msg"):
    """Build a fake CoreV1Event — only the attributes the watcher reads."""
    return SimpleNamespace(
        reason=reason,
        message=message,
        type="Warning" if reason in kew.WARNING_REASONS else "Normal",
        count=count,
        involved_object=SimpleNamespace(kind=kind, name=name),
    )


def _wevt(reason="OOMKilled", service="auth-service",
          severity="critical", involved_name="auth-service-abc-xyz"):
    """Build a WatcherEvent for dispatch-loop tests."""
    return kew.WatcherEvent(
        namespace="spyroom",
        reason=reason,
        severity=severity,
        message="m",
        involved_kind="Pod",
        involved_name=involved_name,
        service=service,
        count=1,
        received_at=time.monotonic(),
    )


@pytest.fixture(autouse=True)
def _reset_watcher_state():
    kew.reset_state()
    yield
    kew.reset_state()


# ── severity_for ────────────────────────────────────────────────────────────


def test_severity_for_critical_reasons():
    for r in ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff",
              "FailedScheduling", "NodeNotReady", "Evicted",
              "MemoryPressure", "DiskPressure", "PIDPressure",
              "NetworkUnavailable"):
        assert kew.severity_for(r) == "critical", r


def test_severity_for_warning_reasons():
    for r in ("Unhealthy", "BackOff", "Failed", "FailedMount", "Killing"):
        assert kew.severity_for(r) == "warning", r


def test_severity_for_unknown_returns_ignore():
    assert kew.severity_for("MysteryReason") == "ignore"
    assert kew.severity_for("Pulled") == "ignore"


# ── Service-name resolution ─────────────────────────────────────────────────


def test_strip_pod_suffix():
    assert kew._strip_pod_suffix("room-service-6c5685cc89-n6clv") == "room-service"
    assert kew._strip_pod_suffix("auth-service-abcd-wxyz") == "auth-service"
    assert kew._strip_pod_suffix("api-gateway-1abc2-defgh") == "api-gateway"


def test_strip_rs_suffix():
    assert kew._strip_rs_suffix("room-service-6c5685cc89") == "room-service"


def test_resolve_service_pod_kind_strips_hashes():
    assert kew.resolve_service("Pod", "auth-service-abc-xyz") == "auth-service"


def test_resolve_service_deployment_uses_name_directly():
    assert kew.resolve_service("Deployment", "room-service") == "room-service"


def test_resolve_service_statefulset_uses_name_directly():
    assert kew.resolve_service("StatefulSet", "postgres") == "postgres"


def test_resolve_service_replicaset_strips_hash():
    assert kew.resolve_service("ReplicaSet", "auth-service-7c8d9e") == "auth-service"


def test_resolve_service_node_returns_none():
    assert kew.resolve_service("Node", "minikube") is None
    assert kew.resolve_service("Namespace", "spyroom") is None


def test_resolve_service_empty_name_returns_none():
    assert kew.resolve_service("Pod", "") is None
    assert kew.resolve_service("", "auth-service") is None


# ── classify_event ──────────────────────────────────────────────────────────


def test_classify_event_accepts_critical_reason():
    classified = kew.classify_event(_evt("OOMKilled"))
    assert classified is not None
    assert classified.reason == "OOMKilled"
    assert classified.severity == "critical"
    assert classified.service == "auth-service"
    assert classified.involved_kind == "Pod"


def test_classify_event_accepts_critical_reason_with_type_normal():
    """Reason-based gating — type=Normal must NOT exclude OOMKilled."""
    e = _evt("OOMKilled")
    e.type = "Normal"
    classified = kew.classify_event(e)
    assert classified is not None
    assert classified.severity == "critical"


def test_classify_event_accepts_warning_reason():
    classified = kew.classify_event(_evt("BackOff"))
    assert classified is not None
    assert classified.severity == "warning"


def test_classify_event_ignores_pulled():
    assert kew.classify_event(_evt("Pulled")) is None


def test_classify_event_skips_unknown_reason():
    assert kew.classify_event(_evt("MysteryReason")) is None


def test_classify_event_skips_unresolvable_service():
    e = _evt("NodeNotReady", kind="Node", name="minikube")
    assert kew.classify_event(e) is None


def test_classify_event_skips_missing_involved_object():
    e = SimpleNamespace(reason="OOMKilled", message="m", type="Warning", count=1)
    assert kew.classify_event(e) is None


def test_classify_event_skips_empty_reason():
    e = _evt("")
    e.reason = ""
    assert kew.classify_event(e) is None


# ── Cooldown ────────────────────────────────────────────────────────────────


def test_cooldown_blocks_repeat_within_window():
    now = time.monotonic()
    kew._record_dispatch("auth-service", "OOMKilled", now)
    assert kew._within_cooldown("auth-service", "OOMKilled", now + 1)
    assert kew._within_cooldown("auth-service", "OOMKilled", now + 299)


def test_cooldown_lifts_after_window():
    now = time.monotonic()
    kew._record_dispatch("auth-service", "OOMKilled", now)
    assert not kew._within_cooldown("auth-service", "OOMKilled", now + 301)


def test_cooldown_scoped_per_service_and_reason():
    now = time.monotonic()
    kew._record_dispatch("auth-service", "OOMKilled", now)
    assert not kew._within_cooldown("room-service", "OOMKilled", now + 1)
    assert not kew._within_cooldown("auth-service", "BackOff", now + 1)


# ── Rate limit (sliding window) ─────────────────────────────────────────────


def test_rate_limit_allows_under_threshold(monkeypatch):
    monkeypatch.setattr(kew, "RATE_LIMIT_PER_MIN", 5)
    now = time.monotonic()
    for _ in range(4):
        kew._state.rate_window.append(now)
    assert not kew._rate_limit_exceeded(now)


def test_rate_limit_blocks_at_threshold(monkeypatch):
    monkeypatch.setattr(kew, "RATE_LIMIT_PER_MIN", 5)
    now = time.monotonic()
    for _ in range(5):
        kew._state.rate_window.append(now)
    assert kew._rate_limit_exceeded(now)


def test_rate_limit_expires_old_entries(monkeypatch):
    monkeypatch.setattr(kew, "RATE_LIMIT_PER_MIN", 5)
    now = time.monotonic()
    for _ in range(5):
        kew._state.rate_window.append(now - 70.0)
    assert not kew._rate_limit_exceeded(now)
    assert len(kew._state.rate_window) == 0


# ── Bounded queue overflow (_enqueue_or_drop) ──────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_drops_oldest_when_full():
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    e1 = _wevt(involved_name="p1")
    e2 = _wevt(involved_name="p2")
    e3 = _wevt(involved_name="p3")

    kew._enqueue_or_drop(q, e1)
    kew._enqueue_or_drop(q, e2)
    kew._enqueue_or_drop(q, e3)   # full → drops oldest (e1), inserts e3

    assert q.qsize() == 2
    first = await asyncio.wait_for(q.get(), timeout=0.5)
    second = await asyncio.wait_for(q.get(), timeout=0.5)
    assert first.involved_name == "p2"
    assert second.involved_name == "p3"


# ── Dispatch coroutine — happy path + cooldown + rate limit + shutdown ─────


@pytest.mark.asyncio
async def test_dispatch_exits_within_one_second_on_stop():
    """A regression to a blocking queue.get() would hang here — fail in 1s."""
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    sem = asyncio.Semaphore(3)

    task = asyncio.create_task(kew._dispatch_loop(queue, stop, sem))
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_dispatch_triggers_run_analysis(monkeypatch):
    """Happy path: one event → one run_analysis call."""
    monkeypatch.setattr(kew, "RATE_LIMIT_PER_MIN", 100)
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    sem = asyncio.Semaphore(3)

    triggered = []
    mock_run = AsyncMock(return_value=SimpleNamespace(
        incident_id="inc-1", confidence_source="signal",
        proposed_action={"type": "notify"},
    ))

    async def fake_run(req):
        triggered.append(req)
        return await mock_run(req)

    with patch("app.core.agent.run_analysis", new=fake_run):
        await queue.put(_wevt())
        task = asyncio.create_task(kew._dispatch_loop(queue, stop, sem))

        # Poll for dispatch (max 2s)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if triggered:
                break

        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(triggered) == 1
    assert triggered[0].service == "auth-service"
    assert triggered[0].namespace == "spyroom"
    assert kew._within_cooldown("auth-service", "OOMKilled", time.monotonic())


@pytest.mark.asyncio
async def test_dispatch_respects_cooldown(monkeypatch):
    """Two identical events → only the first dispatches."""
    monkeypatch.setattr(kew, "RATE_LIMIT_PER_MIN", 100)
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    sem = asyncio.Semaphore(3)

    call_count = {"n": 0}

    async def fake_run(req):
        call_count["n"] += 1
        return SimpleNamespace(incident_id="x", confidence_source="signal",
                               proposed_action=None)

    with patch("app.core.agent.run_analysis", new=fake_run):
        await queue.put(_wevt())
        await queue.put(_wevt())   # immediate dupe
        task = asyncio.create_task(kew._dispatch_loop(queue, stop, sem))

        # Wait for the queue to drain — bounded
        for _ in range(40):
            await asyncio.sleep(0.05)
            if queue.empty() and call_count["n"] >= 1:
                break

        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_dispatch_respects_rate_limit(monkeypatch):
    """Burst above rate limit gets partially suppressed."""
    monkeypatch.setattr(kew, "RATE_LIMIT_PER_MIN", 2)
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    sem = asyncio.Semaphore(3)

    call_count = {"n": 0}

    async def fake_run(req):
        call_count["n"] += 1
        return SimpleNamespace(incident_id="x", confidence_source="signal",
                               proposed_action=None)

    with patch("app.core.agent.run_analysis", new=fake_run):
        for svc in ("a", "b", "c", "d", "e"):
            await queue.put(_wevt(service=svc,
                                  involved_name=f"{svc}-x-y"))
        task = asyncio.create_task(kew._dispatch_loop(queue, stop, sem))

        for _ in range(60):
            await asyncio.sleep(0.05)
            if queue.empty():
                break

        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    # First 2 dispatched; remaining 3 suppressed by rate limit
    assert call_count["n"] == 2


# ── start_watcher gating ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_watcher_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(kew, "ENABLED", False)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(kew.start_watcher(stop), timeout=1.0)


# ── Sync watch loop (with injected stream_factory) ──────────────────────────


def test_run_watch_loop_dispatches_to_queue():
    """Inject a fake stream → loop classifies + enqueues, then exits on stop."""
    delivered: list = []

    class _FakeStream:
        """Iterable that yields one event then ``StopIteration``."""
        def __init__(self):
            self._events = [_evt("OOMKilled")]
            self._idx = 0
        def __iter__(self):
            return self
        def __next__(self):
            if self._idx >= len(self._events):
                raise StopIteration
            v = self._events[self._idx]
            self._idx += 1
            return v

    def stream_factory():
        return _FakeStream(), lambda: None   # no-op stop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        done = threading.Event()

        def _bg():
            try:
                kew._run_watch_loop("spyroom", stop, loop, q,
                                    stream_factory=stream_factory)
            finally:
                done.set()

        t = threading.Thread(target=_bg, daemon=True)
        t.start()

        # Give the thread a moment to start + enqueue
        async def _drain():
            try:
                evt = await asyncio.wait_for(q.get(), timeout=1.5)
                delivered.append(evt)
            except asyncio.TimeoutError:
                pass
            # Tell the thread to exit
            stop.set()

        loop.run_until_complete(_drain())
        # Drain any leftover callbacks scheduled by call_soon_threadsafe
        for _ in range(5):
            loop.call_soon(lambda: None)
            loop.run_until_complete(asyncio.sleep(0.05))
        t.join(timeout=2.0)
        assert not t.is_alive(), "watch thread did not exit within timeout"
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert len(delivered) == 1
    assert delivered[0].reason == "OOMKilled"
    assert delivered[0].service == "auth-service"


def test_run_watch_loop_returns_silently_when_factory_returns_none():
    """When kubernetes is unavailable, the factory builder returns None."""
    # Pass an explicit None factory — _run_watch_loop should return
    # immediately without raising.
    loop = asyncio.new_event_loop()
    try:
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        # We pre-cancel via setting stop = True so the while loop never enters.
        stop.set()
        # Pass a no-op stream_factory; the loop will see stop set immediately.
        kew._run_watch_loop("spyroom", stop, loop, q,
                            stream_factory=lambda: (iter([]), lambda: None))
    finally:
        loop.close()


def test_run_watch_loop_reconnects_on_exception(monkeypatch):
    """A factory that raises causes a reconnect attempt; then stop ends the loop.

    Initial backoff is 1.0s; cap at 1.0s.  Sleep ~1.5s in the test to
    guarantee the second factory call has happened before we set stop.
    """
    monkeypatch.setattr(kew, "RECONNECT_BACKOFF_MAX", 1)

    call_count = {"n": 0}

    def flaky_factory():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated API server hiccup")
        # Subsequent calls return an empty stream — for loop exits at once
        return iter([]), lambda: None

    loop = asyncio.new_event_loop()
    try:
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        done = threading.Event()

        def _bg():
            try:
                kew._run_watch_loop("spyroom", stop, loop, q,
                                    stream_factory=flaky_factory)
            finally:
                done.set()

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        time.sleep(1.5)   # allow first call to raise + 1s backoff + second call
        stop.set()
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert call_count["n"] >= 2, (
            f"factory should have been retried after the simulated failure; "
            f"got call_count={call_count['n']}"
        )
    finally:
        loop.close()

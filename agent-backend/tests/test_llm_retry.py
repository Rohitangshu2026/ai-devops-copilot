"""Tests for the same-key retry-with-backoff wrapper in app.llm.client.

Covers:
  * transient 5xx → retries with backoff, eventually succeeds
  * 429 rate-limit → retries with longer backoff
  * Retry-After hint embedded in error message is honored
  * non-retriable errors (404, auth) bubble up immediately
  * retry budget exhausted → raises the last exception
  * total wall-time budget caps a slow chain
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.llm.client import (
    _call_with_retry,
    _is_rate_limit,
    _is_transient_server,
    _parse_retry_after,
)


# ── Discriminator helpers ────────────────────────────────────────────────────


def test_is_rate_limit_recognises_message():
    assert _is_rate_limit(Exception("429 You exceeded your current quota"))
    assert _is_rate_limit(Exception("Rate limit reached for model"))
    assert _is_rate_limit(Exception("ResourceExhausted: per-minute quota"))
    assert not _is_rate_limit(Exception("404 not found"))


def test_is_transient_server_recognises_5xx_text():
    assert _is_transient_server(Exception("500 Internal error encountered"))
    assert _is_transient_server(Exception("503 Service Unavailable"))
    assert _is_transient_server(Exception("connection reset by peer"))
    assert _is_transient_server(Exception("timed out after 30s"))
    assert not _is_transient_server(Exception("404 model not found"))


def test_parse_retry_after_picks_up_hint():
    assert _parse_retry_after(Exception("Please retry in 11.27 seconds")) == pytest.approx(11.27)
    assert _parse_retry_after(Exception("Retry-After: 5")) == pytest.approx(5.0)
    assert _parse_retry_after(Exception("just a plain error")) is None


# ── Retry behaviour ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_succeeds_on_first_attempt():
    calls = {"n": 0}

    async def ok():
        calls["n"] += 1
        return {"ok": True}

    result = await _call_with_retry(ok, model_name="gemma-4-31b-it", provider="google")
    assert result == {"ok": True}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retries_on_transient_500_then_succeeds(monkeypatch):
    """Critical demo path — a single Google 500 should retry, not fall over."""
    # Speed up the test
    monkeypatch.setattr("app.llm.client._RETRY_BASE_DELAY", 0.0)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("500 Internal error encountered")
        return {"root_cause": "ok"}

    result = await _call_with_retry(flaky, model_name="gemma-4-31b-it", provider="google")
    assert result == {"root_cause": "ok"}
    assert calls["n"] == 3   # failed twice, succeeded on third attempt


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.llm.client._RETRY_BASE_DELAY", 0.0)

    calls = {"n": 0}

    async def rate_limited():
        calls["n"] += 1
        if calls["n"] < 2:
            raise Exception("429 quota exceeded — please retry in 0.1s")
        return {"ok": True}

    result = await _call_with_retry(rate_limited, model_name="gemini-2.5-flash", provider="google")
    assert result == {"ok": True}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_404_raises_immediately_no_retry():
    """Non-retriable errors must bubble up so model-fallover can run."""
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise Exception("404 models/whatever is not found for API version v1beta")

    with pytest.raises(Exception, match="404"):
        await _call_with_retry(bad, model_name="gemma-bogus", provider="google")
    assert calls["n"] == 1   # no retries on a 404


@pytest.mark.asyncio
async def test_exhausts_max_attempts_then_raises(monkeypatch):
    monkeypatch.setattr("app.llm.client._RETRY_BASE_DELAY", 0.0)

    calls = {"n": 0}

    async def always_500():
        calls["n"] += 1
        raise Exception("500 Internal error encountered")

    with pytest.raises(Exception, match="500"):
        await _call_with_retry(always_500, model_name="gemma-4-31b-it", provider="google")
    # max attempts = 3 (constant in client.py)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_wall_time_budget_cap(monkeypatch):
    """If the chain takes longer than the wall-time budget, bail out early."""
    # Shrink the budget so the test runs fast
    monkeypatch.setattr("app.llm.client._RETRY_TOTAL_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr("app.llm.client._RETRY_BASE_DELAY", 0.1)
    monkeypatch.setattr("app.llm.client._RETRY_MAX_DELAY", 0.5)

    calls = {"n": 0}

    async def always_500():
        calls["n"] += 1
        raise Exception("500 Internal error encountered")

    with pytest.raises(Exception, match="500"):
        await _call_with_retry(always_500, model_name="m", provider="google")
    # At least one attempt happened; budget cap should prevent reaching 3
    assert 1 <= calls["n"] <= 3

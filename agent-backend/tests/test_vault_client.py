"""Tests for Phase 10/11 — HashiCorp Vault client (app/utils/vault_client.py)."""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vault_response(data: dict) -> MagicMock:
    """Simulate a urllib response with KV v2 body."""
    body = {"data": {"data": data}}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _reset_vault_module():
    """Reset module-level cache between tests."""
    import app.utils.vault_client as vc
    vc._secrets_cache = None


# ---------------------------------------------------------------------------
# _fetch_from_vault
# ---------------------------------------------------------------------------


def test_fetch_returns_empty_when_no_addr():
    """Returns empty dict when VAULT_ADDR is not set."""
    _reset_vault_module()
    import app.utils.vault_client as vc
    original_addr = vc._VAULT_ADDR
    vc._VAULT_ADDR = ""
    try:
        result = vc._fetch_from_vault()
    finally:
        vc._VAULT_ADDR = original_addr
    assert result == {}


def test_fetch_returns_empty_when_no_token():
    """Returns empty dict when VAULT_TOKEN is not set."""
    _reset_vault_module()
    import app.utils.vault_client as vc
    original_token = vc._VAULT_TOKEN
    vc._VAULT_TOKEN = ""
    try:
        result = vc._fetch_from_vault()
    finally:
        vc._VAULT_TOKEN = original_token
    assert result == {}


def test_fetch_returns_secrets_on_success():
    """Parses KV v2 response and returns secret dict."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    expected = {"LLM_API_KEY": "sk-test", "GOOGLE_API_KEYS": "gkey"}
    mock_resp = _make_vault_response(expected)

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = vc._fetch_from_vault()

    assert result == expected


def test_fetch_returns_empty_on_network_error():
    """Returns empty dict on any network failure (graceful degradation)."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        result = vc._fetch_from_vault()

    assert result == {}


def test_fetch_returns_empty_on_invalid_json():
    """Returns empty dict when Vault returns non-JSON body."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = vc._fetch_from_vault()

    assert result == {}


def test_fetch_handles_missing_data_key():
    """Returns empty dict when response has unexpected structure."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"meta": "something"}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = vc._fetch_from_vault()

    assert result == {}


# ---------------------------------------------------------------------------
# get_vault_secrets — caching behaviour
# ---------------------------------------------------------------------------


def test_get_vault_secrets_caches_result():
    """Second call returns cached value without hitting Vault again."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    expected = {"LLM_API_KEY": "cached-key"}
    mock_resp = _make_vault_response(expected)

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        first = vc.get_vault_secrets()
        second = vc.get_vault_secrets()

    assert first == expected
    assert second == expected
    # urlopen should only be called once
    assert mock_urlopen.call_count == 1


def test_get_vault_secrets_force_refresh():
    """force_refresh=True bypasses cache and re-fetches."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    first_data = {"LLM_API_KEY": "old-key"}
    second_data = {"LLM_API_KEY": "new-key"}

    call_count = 0

    def _side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_vault_response(first_data)
        return _make_vault_response(second_data)

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch("urllib.request.urlopen", side_effect=_side_effect):
        first = vc.get_vault_secrets()
        second = vc.get_vault_secrets(force_refresh=True)

    assert first == first_data
    assert second == second_data


def test_get_vault_secrets_returns_empty_when_no_vault():
    """Returns empty dict when Vault is not configured."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    with patch.object(vc, "_VAULT_ADDR", ""), \
         patch.object(vc, "_VAULT_TOKEN", ""):
        result = vc.get_vault_secrets()

    assert result == {}


# ---------------------------------------------------------------------------
# get_secret — convenience wrapper
# ---------------------------------------------------------------------------


def test_get_secret_returns_value():
    """Returns specific secret by key."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    vc._secrets_cache = {"API_KEY": "my-key", "MODEL": "gpt-4"}
    assert vc.get_secret("API_KEY") == "my-key"
    assert vc.get_secret("MODEL") == "gpt-4"


def test_get_secret_returns_default_on_missing_key():
    """Returns default value when key not in secrets."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    vc._secrets_cache = {}
    assert vc.get_secret("MISSING_KEY") == ""
    assert vc.get_secret("MISSING_KEY", "fallback") == "fallback"


def test_get_secret_uses_cache():
    """Does not fetch from Vault if cache is already populated."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    vc._secrets_cache = {"CACHED": "value"}
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = vc.get_secret("CACHED")

    assert result == "value"
    mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Secret path construction
# ---------------------------------------------------------------------------


def test_fetch_constructs_correct_url():
    """URL is constructed as {VAULT_ADDR}/v1/{VAULT_SECRET_PATH}."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    mock_resp = _make_vault_response({})
    captured_urls = []

    def _capture_url(request, timeout=None):
        captured_urls.append(request.full_url)
        return mock_resp

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch.object(vc, "_VAULT_SECRET_PATH", "secret/data/llm-credentials"), \
         patch("urllib.request.urlopen", side_effect=_capture_url):
        vc._fetch_from_vault()

    assert len(captured_urls) == 1
    assert captured_urls[0] == "http://vault:8200/v1/secret/data/llm-credentials"


def test_fetch_strips_trailing_slash_from_addr():
    """Trailing slash in VAULT_ADDR does not double-slash the URL."""
    _reset_vault_module()
    import app.utils.vault_client as vc

    mock_resp = _make_vault_response({})
    captured_urls = []

    def _capture_url(request, timeout=None):
        captured_urls.append(request.full_url)
        return mock_resp

    with patch.object(vc, "_VAULT_ADDR", "http://vault:8200/"), \
         patch.object(vc, "_VAULT_TOKEN", "root-token"), \
         patch.object(vc, "_VAULT_SECRET_PATH", "secret/data/creds"), \
         patch("urllib.request.urlopen", side_effect=_capture_url):
        vc._fetch_from_vault()

    assert "//" not in captured_urls[0].replace("http://", "").replace("https://", "")

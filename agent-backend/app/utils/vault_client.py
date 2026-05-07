"""HashiCorp Vault integration for secure secret retrieval (Phase 10/11).

Fetches secrets from Vault's KV v2 store at startup and makes them
available as a dict.  Falls back silently to empty dict when Vault is
unreachable, allowing environment variables (llm-credentials Secret) to
take precedence.

Vault address and token are read from VAULT_ADDR / VAULT_TOKEN env vars
(injected via the Kubernetes Deployment manifest).  In local docker-compose
mode both vars are absent, so the fallback always applies.

Secret path: ``secret/data/llm-credentials`` (KV v2 mount ``secret``).
"""
from __future__ import annotations

import os
from typing import Any

from app.utils.logger import get_logger

logger = get_logger("vault_client")

_VAULT_ADDR       = os.getenv("VAULT_ADDR", "")
_VAULT_TOKEN      = os.getenv("VAULT_TOKEN", "")
_VAULT_SECRET_PATH = os.getenv("VAULT_SECRET_PATH", "secret/data/llm-credentials")

# Module-level cache so we only hit Vault once per pod lifecycle.
_secrets_cache: dict[str, str] | None = None


def _fetch_from_vault() -> dict[str, str]:
    """Perform a single synchronous HTTP GET against the Vault KV v2 API.

    Returns an empty dict on any failure (connection error, auth failure,
    missing secret).  All failures are logged at WARNING level — the caller
    is expected to fall back to environment variables.
    """
    if not _VAULT_ADDR or not _VAULT_TOKEN:
        return {}

    try:
        import urllib.request
        import json

        url = f"{_VAULT_ADDR.rstrip('/')}/v1/{_VAULT_SECRET_PATH.lstrip('/')}"
        req = urllib.request.Request(
            url,
            headers={"X-Vault-Token": _VAULT_TOKEN},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read())

        # KV v2 nests the actual data under data.data
        data = body.get("data", {}).get("data", {})
        if data:
            logger.info({
                "message": "vault_secrets_loaded",
                "path": _VAULT_SECRET_PATH,
                "keys": list(data.keys()),
            })
        return {k: str(v) for k, v in data.items()}

    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "message": "vault_fetch_failed",
            "addr": _VAULT_ADDR,
            "path": _VAULT_SECRET_PATH,
            "error": str(exc),
        })
        return {}


def get_vault_secrets(force_refresh: bool = False) -> dict[str, str]:
    """Return cached Vault secrets (fetched once at first call).

    Args:
        force_refresh: Re-fetch from Vault even if cache is populated.

    Returns:
        Dict of secret key → value.  Empty dict when Vault is unreachable.
    """
    global _secrets_cache
    if _secrets_cache is None or force_refresh:
        _secrets_cache = _fetch_from_vault()
    return _secrets_cache


def get_secret(key: str, default: str = "") -> str:
    """Return a single secret value by key, falling back to *default*."""
    return get_vault_secrets().get(key, default)

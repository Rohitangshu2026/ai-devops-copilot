"""Application settings with Vault-first secret resolution (Phase 10).

Resolution order for LLM API key fields:
  1. HashiCorp Vault (VAULT_ADDR + VAULT_TOKEN env vars present → fetch KV secret)
  2. Environment variables / .env file (standard pydantic-settings behaviour)
  3. Empty string default (validated at call time by the LLM client)

This means: in Kubernetes with Vault deployed, secrets are centralised and
rotated in Vault.  In local docker-compose (no Vault), env vars in .env
take effect normally.  The application never needs to be redeployed to
rotate an API key — just update the Vault secret.
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    es_url: str = "http://localhost:9200"
    es_index: str = "devops-logs-*"

    # Provider-specific API keys — comma-separated, rotated on rate-limit.
    # Vault takes precedence when reachable; env vars are the fallback.
    llm_api_key: str = ""
    google_api_keys: str = ""
    anthropic_api_keys: str = ""
    openai_api_keys: str = ""

    # Default model — must resolve against Google's public Generative AI API.
    # "gemma-4-31b-it" historically defaulted here but is not exposed publicly
    # on the v1beta endpoint, so calls 404 with no key/quota rotation possible.
    # Override per-deployment via LLM_MODEL env var or the llm-credentials Secret.
    llm_model: str = "gemini-1.5-flash"
    llm_model_fallback: str = ""

    environment: str = "dev"

    # Vault settings (injected via k8s Deployment env vars)
    vault_addr: str = ""
    vault_token: str = ""
    vault_secret_path: str = "secret/data/llm-credentials"

    # Human approval workflow (Phase 11)
    approval_secret_key: str = "change-me-in-production"
    approval_expiry_seconds: int = 300

    # Slack integration (Phase 11) — leave blank to disable
    slack_webhook_url: str = ""

    # Admin API key — protects /admin/* and /services/*/unfreeze endpoints
    admin_api_key: str = ""

    # ── Multi-platform refactor ──────────────────────────────────────────
    # Directory containing platform yaml files.  The registry seeds itself
    # with a default fallback when this is absent, so tests and clean
    # checkouts keep working without any configs.
    platforms_dir: str = "configs/platforms"

    # ── GitLab integration (Phase 11 + multi-platform refactor) ──────────
    # X-Gitlab-Token shared secret used to authenticate incoming webhooks.
    # Generate with: openssl rand -hex 32
    gitlab_webhook_token: str = ""
    # Read-only API access for posting MR comments back to GitLab projects.
    gitlab_api_url: str = "https://gitlab.com"
    gitlab_api_token: str = ""

    # `extra="ignore"` — be tolerant of legacy env-var spellings (e.g. the
    # singular GOOGLE_API_KEY some operators set instead of GOOGLE_API_KEYS).
    # Without this, pydantic-settings v2 raises ValidationError at startup
    # and the pod crash-loops with no LLM availability.  We compensate by
    # mapping known singular aliases below in the post-init step.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def _apply_vault_overrides(s: Settings) -> Settings:
    """Overlay Vault secrets onto settings fields (non-destructive).

    Only overrides a field when Vault returns a non-empty value for that key,
    preserving any env-var value when Vault is unavailable or the key is absent.
    """
    if not s.vault_addr or not s.vault_token:
        return s

    try:
        from app.utils.vault_client import get_vault_secrets
        secrets = get_vault_secrets()
        if secrets.get("GOOGLE_API_KEYS"):
            s.google_api_keys = secrets["GOOGLE_API_KEYS"]
        if secrets.get("ANTHROPIC_API_KEYS"):
            s.anthropic_api_keys = secrets["ANTHROPIC_API_KEYS"]
        if secrets.get("OPENAI_API_KEYS"):
            s.openai_api_keys = secrets["OPENAI_API_KEYS"]
        if secrets.get("LLM_API_KEY"):
            s.llm_api_key = secrets["LLM_API_KEY"]
        if secrets.get("LLM_MODEL"):
            s.llm_model = secrets["LLM_MODEL"]
    except Exception:  # noqa: BLE001
        pass  # Vault unavailable — env vars remain in effect

    return s


def _apply_singular_aliases(s: Settings) -> Settings:
    """Backfill plural key-list fields from the singular env vars.

    Some operators set ``GOOGLE_API_KEY=<single key>`` instead of the
    comma-separated ``GOOGLE_API_KEYS=<key1,key2,...>`` that the rotation
    code expects.  Read the singular form from ``os.environ`` and use it as
    the fallback so a single key still works without any config change.
    """
    if not s.google_api_keys:
        single = os.environ.get("GOOGLE_API_KEY", "").strip()
        if single:
            s.google_api_keys = single
    if not s.anthropic_api_keys:
        single = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if single:
            s.anthropic_api_keys = single
    if not s.openai_api_keys:
        single = os.environ.get("OPENAI_API_KEY", "").strip()
        if single:
            s.openai_api_keys = single
    return s


settings: Settings = _apply_singular_aliases(_apply_vault_overrides(Settings()))

"""Platform configuration loader and registry.

A *platform* is a logical grouping of services that share an ownership
boundary — typically corresponding to one application repository.  The
registry loads ``configs/platforms/*.yaml`` at startup, validates each file
against a pydantic schema, and exposes lookup helpers used by the analysis
pipeline:

  * ``PlatformRegistry.get(name)`` — fetch by platform name
  * ``PlatformRegistry.for_service(service)`` — reverse lookup
  * ``PlatformRegistry.get_by_gitlab_project(project)`` — webhook routing
  * ``PlatformRegistry.all()`` — iterate registered platforms

Behaviour notes:

* Validation failures **do not** crash startup.  The offending file is logged
  and skipped — the registry stays usable with the platforms that did parse.
  (This matches ``policy.py``'s "skip-but-log" stance for non-critical config.)
* When no yaml files are present (clean checkout, tests, etc.) the registry
  seeds itself with a single fallback platform equivalent to the existing
  hardcoded behaviour, so legacy callers that pass plain ``service`` /
  ``environment`` keep working untouched.
* Reload is SIGHUP-driven, mirroring ``policy.reload_policy()``.  See
  ``app/main.py`` for the signal handler wire-up.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from app.utils.logger import get_logger

logger = get_logger("platforms")

# ── Schema types ─────────────────────────────────────────────────────────────


class ServiceSpec(BaseModel):
    """A single service inside a platform.

    The shape is intentionally minimal — anything that varies *per pod* (image
    tag, replicas, resource limits) belongs in k8s manifests, not here.  This
    file holds only what the *analysis* layer needs to reason about the
    service: its name, dependencies, and risk class.
    """

    name: str
    language: str = ""                     # "java" | "python" | "node" | ""
    log_format: str = "json"               # "json" | "logfmt" | "plain"
    depends_on: List[str] = Field(default_factory=list)
    criticality: str = "medium"            # low | medium | high | critical
    deployment_kind: str = "Deployment"    # for kubectl get/set
    container_name: str = ""               # override if container != service name
    description: str = ""

    @field_validator("criticality")
    @classmethod
    def _check_criticality(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "critical"}
        if v not in allowed:
            raise ValueError(f"criticality must be one of {allowed}, got {v!r}")
        return v


class PlatformConfig(BaseModel):
    """One platform = one ownership boundary (typically one repo)."""

    name: str
    namespace: str
    environments: List[str] = Field(default_factory=lambda: ["dev"])
    dry_run_envs: List[str] = Field(default_factory=lambda: ["dev"])

    # Where this platform's logs land in Elasticsearch.  Defaults to the
    # shared index pattern used by the existing single-platform deployment.
    log_index_pattern: str = "devops-logs-*"
    log_service_field: str = "service"     # ES field that holds the service name

    services: List[ServiceSpec] = Field(default_factory=list)

    # Cross-repo wiring metadata
    gitlab_project: Optional[str] = None   # e.g. "spe-group2/spyroom-platform"
    repo_url: Optional[str] = None
    owners: List[str] = Field(default_factory=list)   # Slack handles
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_validator("environments", "dry_run_envs")
    @classmethod
    def _non_empty_list(cls, v: List[str]) -> List[str]:
        return v or ["dev"]

    @field_validator("dry_run_envs")
    @classmethod
    def _dry_run_subset(cls, v: List[str], info) -> List[str]:
        envs = info.data.get("environments") or ["dev"]
        # Don't raise — just keep the intersection.  Better to be permissive
        # than fail-loud on what is essentially a hint.
        return [e for e in v if e in envs] or [envs[0]]

    # Convenience helpers --------------------------------------------------

    def service_names(self) -> List[str]:
        return [s.name for s in self.services]

    def get_service(self, name: str) -> Optional[ServiceSpec]:
        for svc in self.services:
            if svc.name == name:
                return svc
        return None

    def is_dry_run(self, environment: str) -> bool:
        return environment in self.dry_run_envs


# ── Registry ─────────────────────────────────────────────────────────────────


class PlatformRegistry:
    """In-memory store of all known platforms."""

    def __init__(self, platforms_dir: str | os.PathLike):
        self._dir = Path(platforms_dir)
        self._platforms: Dict[str, PlatformConfig] = {}
        self.load()

    # -- loading -----------------------------------------------------------

    def load(self) -> None:
        self._platforms = {}
        if not self._dir.exists():
            logger.info({
                "message": "platforms_dir_missing",
                "dir": str(self._dir),
                "hint": "Using built-in fallback platform only",
            })
            self._seed_fallback()
            return

        for path in sorted(self._dir.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text()) or {}
                cfg = PlatformConfig(**raw)
                if cfg.name in self._platforms:
                    logger.warning({
                        "message": "platform_duplicate_name",
                        "name": cfg.name,
                        "file": str(path),
                    })
                self._platforms[cfg.name] = cfg
                logger.info({
                    "message": "platform_loaded",
                    "name": cfg.name,
                    "namespace": cfg.namespace,
                    "services": [s.name for s in cfg.services],
                    "file": str(path),
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning({
                    "message": "platform_load_failed",
                    "file": str(path),
                    "error": str(exc),
                })

        if not self._platforms:
            self._seed_fallback()

    def reload(self) -> None:
        """Re-read all yaml files.  Triggered by SIGHUP."""
        logger.info({"message": "platform_registry_reload"})
        self.load()

    def _seed_fallback(self) -> None:
        """Built-in defaults preserving the pre-refactor behaviour.

        This guarantees the registry is always non-empty so downstream code
        can call ``for_service`` without worrying about an absent yaml file.
        """
        default = PlatformConfig(
            name="default",
            namespace="default",
            environments=["dev", "staging", "production"],
            dry_run_envs=["dev"],
            log_index_pattern="devops-logs-*",
            services=[
                ServiceSpec(name="sample-app", depends_on=["elasticsearch"]),
                ServiceSpec(name="agent-backend", depends_on=["elasticsearch"]),
            ],
        )
        self._platforms[default.name] = default

    # -- accessors ---------------------------------------------------------

    def get(self, name: str) -> Optional[PlatformConfig]:
        return self._platforms.get(name)

    def all(self) -> List[PlatformConfig]:
        return list(self._platforms.values())

    def names(self) -> List[str]:
        return list(self._platforms.keys())

    def for_service(self, service: str) -> Optional[PlatformConfig]:
        """Return the first platform that declares *service* in its services list."""
        for cfg in self._platforms.values():
            if any(s.name == service for s in cfg.services):
                return cfg
        return None

    def get_by_gitlab_project(self, project: str) -> Optional[PlatformConfig]:
        """Look up a platform by its GitLab project path (e.g. 'group/repo').

        Used by the webhook handler to map an incoming pipeline-failure event
        to the right platform/namespace.
        """
        if not project:
            return None
        normalized = project.strip().lower()
        for cfg in self._platforms.values():
            target = (cfg.gitlab_project or "").strip().lower()
            if target and (target == normalized or target.endswith("/" + normalized) or normalized.endswith("/" + target)):
                return cfg
            # Also match plain repo name (last path segment)
            if target.rsplit("/", 1)[-1] == normalized.rsplit("/", 1)[-1] and target:
                return cfg
        return None

    def resolve(
        self,
        *,
        platform: Optional[str] = None,
        service: Optional[str] = None,
    ) -> PlatformConfig:
        """Resolution chain: explicit platform → service lookup → default fallback.

        Always returns a usable :class:`PlatformConfig` (never None) so the
        analysis pipeline can rely on it without conditional guards.
        """
        if platform:
            cfg = self.get(platform)
            if cfg is not None:
                return cfg
            logger.info({
                "message": "platform_not_found_falling_back",
                "requested": platform,
            })
        if service:
            cfg = self.for_service(service)
            if cfg is not None:
                return cfg
        # Last resort
        return self.get("default") or next(iter(self._platforms.values()))


# ── Module-level singleton ───────────────────────────────────────────────────

_registry_singleton: Optional[PlatformRegistry] = None


def get_registry(platforms_dir: Optional[str] = None) -> PlatformRegistry:
    """Lazy module-level registry, mirroring ``policy.get_policy()`` style.

    The directory is resolved on first call from:
      1. explicit argument
      2. ``PLATFORMS_DIR`` env var
      3. ``settings.platforms_dir`` (when settings are importable)
      4. ``configs/platforms`` relative to repo root
    """
    global _registry_singleton
    if _registry_singleton is not None:
        return _registry_singleton

    resolved_dir = platforms_dir or os.environ.get("PLATFORMS_DIR", "")
    if not resolved_dir:
        try:
            from app.utils.config import settings  # local import to avoid cycles
            resolved_dir = getattr(settings, "platforms_dir", "") or ""
        except Exception:  # noqa: BLE001
            resolved_dir = ""
    if not resolved_dir:
        resolved_dir = str(
        Path(__file__).resolve().parent.parent.parent
        / "configs"
        / "platforms"
    )

    _registry_singleton = PlatformRegistry(resolved_dir)
    return _registry_singleton


def reload_registry() -> PlatformRegistry:
    """Force-reload of the module-level registry (SIGHUP handler entry point)."""
    reg = get_registry()
    reg.reload()
    return reg

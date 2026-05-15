"""Tests for the multi-platform refactor.

Covers:
  * Loading platform yaml files from disk and validating them.
  * Reverse-lookup by service name + by GitLab project path.
  * Fallback chain when neither the platform nor the service is registered.
  * Dependency-map projection used by ``app.core.causality``.
  * The new ``/api/v1/platforms`` and ``/api/v1/platforms/{name}`` endpoints.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.platforms.registry import PlatformConfig, PlatformRegistry, ServiceSpec


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_yaml(dir_: Path, name: str, body: str) -> None:
    (dir_ / f"{name}.yaml").write_text(textwrap.dedent(body).lstrip())


@pytest.fixture
def fresh_registry_dir(tmp_path: Path) -> Path:
    """Create a clean directory of platform yamls for a single test."""
    _write_yaml(tmp_path, "default", """
        name: default
        namespace: default
        services: []
    """)
    _write_yaml(tmp_path, "spyroom", """
        name: spyroom
        namespace: spyroom
        environments: [dev, production]
        dry_run_envs: [dev]
        gitlab_project: spe-group2/spyroom-platform
        services:
          - name: api-gateway
            depends_on: [auth-service, room-service]
            criticality: high
          - name: auth-service
            depends_on: [postgres]
            criticality: critical
          - name: room-service
            depends_on: [postgres, redis]
            criticality: high
    """)
    return tmp_path


# ── PlatformConfig / ServiceSpec schema ─────────────────────────────────────


def test_service_spec_default_criticality_is_medium():
    s = ServiceSpec(name="x")
    assert s.criticality == "medium"


def test_service_spec_rejects_unknown_criticality():
    with pytest.raises(Exception):
        ServiceSpec(name="x", criticality="ultra")


def test_platform_config_defaults():
    p = PlatformConfig(name="t", namespace="t")
    assert p.environments == ["dev"]
    assert p.dry_run_envs == ["dev"]
    assert p.log_index_pattern == "devops-logs-*"
    assert p.log_service_field == "service"


def test_platform_dry_run_envs_filtered_to_subset():
    p = PlatformConfig(
        name="t", namespace="t",
        environments=["dev"], dry_run_envs=["dev", "production"],
    )
    # production not in environments, so it gets filtered out
    assert p.dry_run_envs == ["dev"]


def test_is_dry_run():
    p = PlatformConfig(name="t", namespace="t", environments=["dev", "production"],
                       dry_run_envs=["dev"])
    assert p.is_dry_run("dev") is True
    assert p.is_dry_run("production") is False


def test_get_service_returns_none_for_missing():
    p = PlatformConfig(name="t", namespace="t",
                       services=[ServiceSpec(name="a")])
    assert p.get_service("a").name == "a"
    assert p.get_service("missing") is None


# ── PlatformRegistry loading + lookup ────────────────────────────────────────


def test_registry_loads_yaml_files(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    assert set(reg.names()) == {"default", "spyroom"}


def test_registry_seeds_fallback_when_empty(tmp_path):
    reg = PlatformRegistry(tmp_path / "nonexistent")
    assert reg.names() == ["default"]
    assert reg.get("default") is not None


def test_registry_for_service_finds_correct_platform(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.for_service("auth-service")
    assert plat is not None
    assert plat.name == "spyroom"


def test_registry_for_service_returns_none_when_unknown(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    assert reg.for_service("nonexistent-service") is None


def test_registry_get_by_gitlab_project_full_path(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.get_by_gitlab_project("spe-group2/spyroom-platform")
    assert plat is not None
    assert plat.name == "spyroom"


def test_registry_get_by_gitlab_project_short_name(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.get_by_gitlab_project("spyroom-platform")
    assert plat is not None
    assert plat.name == "spyroom"


def test_registry_resolve_explicit_platform(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.resolve(platform="spyroom")
    assert plat.name == "spyroom"


def test_registry_resolve_falls_back_to_service_lookup(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.resolve(service="auth-service")
    assert plat.name == "spyroom"


def test_registry_resolve_falls_back_to_default(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.resolve(service="unknown-service")
    assert plat.name == "default"


def test_registry_resolve_unknown_platform_falls_through_to_service(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    plat = reg.resolve(platform="nonexistent", service="auth-service")
    assert plat.name == "spyroom"


def test_registry_skips_malformed_yaml(tmp_path):
    _write_yaml(tmp_path, "ok", """
        name: good
        namespace: g
    """)
    (tmp_path / "broken.yaml").write_text("this: is: not: valid: yaml: at all")
    reg = PlatformRegistry(tmp_path)
    # "good" still loads, "broken" is skipped — does NOT crash the registry
    assert "good" in reg.names()
    assert "broken" not in reg.names()


def test_registry_reload_picks_up_new_files(fresh_registry_dir):
    reg = PlatformRegistry(fresh_registry_dir)
    assert "newcomer" not in reg.names()

    _write_yaml(fresh_registry_dir, "newcomer", """
        name: newcomer
        namespace: nc
    """)
    reg.reload()
    assert "newcomer" in reg.names()


# ── ServiceRegistry / dependency_map ────────────────────────────────────────


def test_dependency_map_aggregates_across_platforms(fresh_registry_dir, monkeypatch):
    # Swap the module-level singleton for this test
    reg = PlatformRegistry(fresh_registry_dir)
    from app.platforms import registry as _reg_mod
    monkeypatch.setattr(_reg_mod, "_registry_singleton", reg)

    from app.platforms.service_registry import dependency_map
    dm = dependency_map()
    assert dm.get("auth-service") == ["postgres"]
    assert set(dm.get("room-service")) == {"postgres", "redis"}
    assert set(dm.get("api-gateway")) == {"auth-service", "room-service"}


def test_namespace_for_resolves_via_registry(fresh_registry_dir, monkeypatch):
    reg = PlatformRegistry(fresh_registry_dir)
    from app.platforms import registry as _reg_mod
    monkeypatch.setattr(_reg_mod, "_registry_singleton", reg)

    from app.platforms.service_registry import namespace_for
    assert namespace_for("auth-service") == "spyroom"
    assert namespace_for("nonexistent") == "default"
    assert namespace_for("nonexistent", default="kube-system") == "kube-system"


# ── API endpoints (/api/v1/platforms) ────────────────────────────────────────


@pytest.fixture
def api_client_with_registry(fresh_registry_dir, monkeypatch):
    """Build a TestClient backed by the temp-dir registry."""
    reg = PlatformRegistry(fresh_registry_dir)
    from app.platforms import registry as _reg_mod
    monkeypatch.setattr(_reg_mod, "_registry_singleton", reg)

    from app.main import app
    return TestClient(app)


def test_list_platforms_endpoint(api_client_with_registry):
    resp = api_client_with_registry.get("/api/v1/platforms")
    assert resp.status_code == 200
    data = resp.json()
    names = [p["name"] for p in data["platforms"]]
    assert set(names) == {"default", "spyroom"}

    spy = next(p for p in data["platforms"] if p["name"] == "spyroom")
    assert spy["namespace"] == "spyroom"
    assert spy["gitlab_project"] == "spe-group2/spyroom-platform"
    svc_names = {s["name"] for s in spy["services"]}
    assert {"api-gateway", "auth-service", "room-service"} <= svc_names


def test_get_platform_endpoint(api_client_with_registry):
    resp = api_client_with_registry.get("/api/v1/platforms/spyroom")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "spyroom"
    assert data["namespace"] == "spyroom"


def test_get_platform_404(api_client_with_registry):
    resp = api_client_with_registry.get("/api/v1/platforms/nonexistent")
    assert resp.status_code == 404

"""Platform registry — multi-platform extension layer.

The DevOps Copilot was originally hardcoded around a single demo service
(``sample-app``). This package adds a thin abstraction over *platforms* — a
named bundle of services, namespaces, environments, dependencies, and
ownership metadata — so the same analysis pipeline can monitor any external
repository (SpyRoom, future fintech/ecommerce apps, etc.) without code
changes.

Public surface:
    PlatformRegistry        — singleton-style accessor (lazy-loaded)
    PlatformConfig          — pydantic-validated platform descriptor
    ServiceSpec             — per-service metadata inside a platform
    get_registry()          — convenience function
"""
from app.platforms.registry import (  # noqa: F401
    PlatformConfig,
    PlatformRegistry,
    ServiceSpec,
    get_registry,
)

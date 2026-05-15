"""Flat service view over the platform registry.

The :class:`PlatformRegistry` is the source of truth for service metadata,
but a few callers (causality validator, blast-radius fallback map, dashboard)
just want a flat ``service_name → ServiceSpec`` lookup that spans every
registered platform.  This module provides exactly that, plus the dependency
map projection used by :mod:`app.core.causality`.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.platforms.registry import PlatformConfig, ServiceSpec, get_registry


def all_services() -> List[ServiceSpec]:
    """Flatten every platform's services into a single list."""
    out: List[ServiceSpec] = []
    for plat in get_registry().all():
        out.extend(plat.services)
    return out


def find_service(name: str) -> Optional[ServiceSpec]:
    """Locate a service by name across every registered platform."""
    for plat in get_registry().all():
        svc = plat.get_service(name)
        if svc is not None:
            return svc
    return None


def find_platform(name: str) -> Optional[PlatformConfig]:
    """Reverse-lookup helper — return the platform that owns *name*."""
    return get_registry().for_service(name)


def dependency_map() -> Dict[str, List[str]]:
    """Aggregate every platform's service dependencies into one map.

    Returned shape is identical to the historical ``DEPENDENCY_MAP`` dict in
    ``app.core.causality`` so callers can use them interchangeably.
    """
    out: Dict[str, List[str]] = {}
    for plat in get_registry().all():
        for svc in plat.services:
            if svc.depends_on:
                # Merge — later entries union with earlier ones to be permissive
                existing = set(out.get(svc.name, []))
                existing.update(svc.depends_on)
                out[svc.name] = sorted(existing)
    return out


def namespace_for(service: str, default: str = "default") -> str:
    """Resolve a service to its owning platform's namespace."""
    plat = find_platform(service)
    return plat.namespace if plat is not None else default

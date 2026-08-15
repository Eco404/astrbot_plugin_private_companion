# -*- coding: utf-8 -*-
"""Shared discovery and lifecycle checks for optional companion bridges."""
from __future__ import annotations

import sys
import time
from typing import Any


_POSITIVE_TTL = 15.0
_NEGATIVE_TTL = 3.0


def _lifecycle_active(api: Any) -> bool:
    """Return whether an API is still usable without requiring new contracts."""
    if api is None:
        return False
    lifecycle = getattr(api, "bridge_lifecycle_status", None)
    if callable(lifecycle):
        try:
            status = lifecycle()
        except Exception:
            return False
        return isinstance(status, dict) and status.get("active") is True

    # Older split plugins do not expose a lifecycle probe. Their status DTO is
    # still enough to avoid calling an instance that is explicitly disabled.
    status_getter = getattr(api, "status", None)
    if callable(status_getter):
        try:
            status = status_getter()
        except TypeError:
            # Some legacy status methods require the host owner. Preserve the
            # pre-resolver compatibility behavior for those APIs.
            return True
        except Exception:
            return False
        if isinstance(status, dict):
            if status.get("active") is False or status.get("enabled") is False:
                return False
    return True


def _module_candidates(module_names: tuple[str, ...]) -> list[Any]:
    suffixes = tuple(name.removeprefix("data.plugins.") for name in module_names)
    candidates: list[Any] = []
    seen: set[int] = set()
    for name in module_names:
        module = sys.modules.get(name)
        if module is not None and id(module) not in seen:
            candidates.append(module)
            seen.add(id(module))
    for name, module in list(sys.modules.items()):
        if module is None or id(module) in seen:
            continue
        if any(name.endswith(suffix) for suffix in suffixes):
            candidates.append(module)
            seen.add(id(module))
    return candidates


def _uncached_resolve(
    owner: Any,
    *,
    module_names: tuple[str, ...],
    getter_name: str,
    star_name: str,
) -> Any | None:
    for module in _module_candidates(module_names):
        getter = getattr(module, getter_name, None)
        try:
            api = getter() if callable(getter) else None
        except Exception:
            api = None
        if api is not None and api is not owner and _lifecycle_active(api):
            return api

    context = getattr(owner, "context", None)
    getter = getattr(context, "get_registered_star", None)
    if callable(getter):
        try:
            metadata = getter(star_name)
            instance = getattr(metadata, "star_cls", None) if metadata is not None else None
            api = getattr(instance, "extension_api", None)
        except Exception:
            api = None
        if api is not None and api is not owner and _lifecycle_active(api):
            return api
    return None


def resolve_external_bridge(
    owner: Any,
    *,
    cache_key: str,
    module_names: tuple[str, ...],
    getter_name: str,
    star_name: str,
) -> Any | None:
    """Resolve an optional plugin API with bounded positive/negative caching."""
    cache = getattr(owner, "_external_bridge_resolver_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(owner, "_external_bridge_resolver_cache", cache)
    now = time.monotonic()
    entry = cache.get(cache_key)
    if isinstance(entry, dict) and now < float(entry.get("expires_at", 0.0) or 0.0):
        api = entry.get("api")
        if api is None or _lifecycle_active(api):
            return api
        cache.pop(cache_key, None)

    api = _uncached_resolve(
        owner,
        module_names=module_names,
        getter_name=getter_name,
        star_name=star_name,
    )
    cache[cache_key] = {
        "api": api,
        "expires_at": now + (_POSITIVE_TTL if api is not None else _NEGATIVE_TTL),
    }
    return api


def invalidate_external_bridge_cache(owner: Any, cache_key: str | None = None) -> None:
    cache = getattr(owner, "_external_bridge_resolver_cache", None)
    if not isinstance(cache, dict):
        return
    if cache_key:
        cache.pop(cache_key, None)
    else:
        cache.clear()

# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from astrbot_plugin_private_companion.external_bridge_resolver import (
    invalidate_external_bridge_cache,
    resolve_external_bridge,
)


def test_resolver_caches_positive_lookup_and_rechecks_lifecycle() -> None:
    calls = 0
    active = {"value": True}

    class Api:
        def bridge_lifecycle_status(self):
            return {"active": active["value"]}

    api = Api()

    def getter():
        nonlocal calls
        calls += 1
        return api

    module_name = "astrbot_plugin_test_bridge.main"
    module = types.ModuleType(module_name)
    module.get_test_api = getter
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        owner = SimpleNamespace()
        assert resolve_external_bridge(
            owner,
            cache_key="test",
            module_names=(module_name,),
            getter_name="get_test_api",
            star_name="astrbot_plugin_test_bridge",
        ) is api
        assert resolve_external_bridge(
            owner,
            cache_key="test",
            module_names=(module_name,),
            getter_name="get_test_api",
            star_name="astrbot_plugin_test_bridge",
        ) is api
        assert calls == 1

        active["value"] = False
        assert resolve_external_bridge(
            owner,
            cache_key="test",
            module_names=(module_name,),
            getter_name="get_test_api",
            star_name="astrbot_plugin_test_bridge",
        ) is None
    finally:
        invalidate_external_bridge_cache(owner)
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_resolver_uses_short_negative_cache_and_registered_star_fallback() -> None:
    owner = SimpleNamespace(
        context=SimpleNamespace(
            get_registered_star=lambda _name: SimpleNamespace(
                star_cls=SimpleNamespace(extension_api=SimpleNamespace(status=lambda: {"enabled": True}))
            )
        )
    )
    assert resolve_external_bridge(
        owner,
        cache_key="registered",
        module_names=("astrbot_plugin_missing_bridge.main",),
        getter_name="get_missing_api",
        star_name="astrbot_plugin_missing_bridge",
    ) is not None

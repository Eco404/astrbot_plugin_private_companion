# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from astrbot_plugin_private_companion.content_companion_bridge import ContentCompanionBridgeMixin


class _LegacyCreative:
    async def _maybe_start_creative_project(self, *, idle_checked=False):
        return "legacy"


class _FallbackCreative(ContentCompanionBridgeMixin, _LegacyCreative):
    context = None


@pytest.mark.asyncio
async def test_content_bridge_falls_back_when_extension_is_missing() -> None:
    host = _FallbackCreative()
    assert await host._maybe_start_creative_project(idle_checked=True) is True
    assert host._content_companion_status()["installed"] is False


@pytest.mark.asyncio
async def test_content_bridge_delegates_to_loaded_extension() -> None:
    calls = []

    class Api:
        def status(self):
            return {"installed": True, "enabled": True, "available": True}

        async def maybe_start_creative_project(self, owner, *, idle_checked=False):
            calls.append((owner, idle_checked))
            return True

    module_name = "astrbot_plugin_content_companion.main"
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = SimpleNamespace(get_content_companion_api=lambda: Api())
    try:
        host = _FallbackCreative()
        assert await host._maybe_start_creative_project(idle_checked=True) is True
        assert calls == [(host, True)]
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous

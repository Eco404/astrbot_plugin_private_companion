from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest


COMPANION_ROOT = Path(__file__).resolve().parents[1]


def _memory_root() -> Path:
    candidates = (
        COMPANION_ROOT.parent / "memory-official",
        COMPANION_ROOT.parent / "memory",
        COMPANION_ROOT.parents[1] / "astrbot_plugin_memory_companion-main",
    )
    for candidate in candidates:
        if (candidate / "core" / "bridge.py").is_file():
            return candidate
    raise unittest.SkipTest("requires a checked-out MemoryCompanion peer repository")


MEMORY_ROOT = _memory_root()


def _load_modules():
    if "astrbot.api" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(
            warning=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        )
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api

    companion_name = "c4_dual_companion"
    companion = types.ModuleType(companion_name)
    companion.__path__ = [str(COMPANION_ROOT)]
    sys.modules[companion_name] = companion
    adapter = importlib.import_module(f"{companion_name}.memory_companion_adapter")

    memory_name = "c4_dual_memory"
    memory = types.ModuleType(memory_name)
    memory.__path__ = [str(MEMORY_ROOT)]
    sys.modules[memory_name] = memory
    core = types.ModuleType(f"{memory_name}.core")
    core.__path__ = [str(MEMORY_ROOT / "core")]
    sys.modules[core.__name__] = core
    bridge = importlib.import_module(f"{memory_name}.core.bridge")
    service = importlib.import_module(f"{memory_name}.core.service")
    store = importlib.import_module(f"{memory_name}.core.store")
    return adapter.MemoryCompanionAdapterMixin, bridge.MemoryCompanionBridge, service.MemoryCompanionService, store.MemoryStore


def test_companion_profile_facade_reads_memory_profile_without_raw_payload_leak():
    mixin, bridge_type, service_type, store_type = _load_modules()

    class Companion(mixin):
        def __init__(self, bridge):
            self.enable_memory_companion_bridge = True
            self.bridge = bridge

        def _memory_companion_bridge_uncached(self):
            return self.bridge

    with tempfile.TemporaryDirectory() as temporary:
        service = object.__new__(service_type)
        service.store = store_type(Path(temporary) / "memory.db")
        service.store.initialize()
        service._schedule_memory_embedding = lambda *args, **kwargs: None
        bridge = bridge_type(service)

        async def run():
            archived = await bridge.record_bot_personal_archive({
                "memory_type": "bot_creative_work",
                "date": "2026-07-30",
                "window": "afternoon",
                "occurred_at": "2026-07-30T15:00:00+08:00",
                "source_refs": ["companion:c4:creative"],
                "idempotency_key": "c4:dual:creative",
                "payload": {"summary": "raw creative detail must remain internal"},
            })
            companion = Companion(bridge)
            result = await companion._memory_companion_read_profile(
                "bot_creative",
                query="creative",
                current_date="2026-07-30",
                current_window="afternoon",
            )
            denied = await companion._memory_companion_read_profile("locked_frame_personal")
            return archived, result, denied

        try:
            archived, result, denied = asyncio.run(run())
            assert archived["ok"] is True
            assert result["ok"] is True and result["read_only"] is True
            assert len(result["items"]) == 1
            assert all("payload" not in item and "content" not in item and "evidence" not in item for item in result["items"])
            assert "raw creative detail" not in str(result)
            assert denied["state"] == "forbidden"
        finally:
            service.store.close()

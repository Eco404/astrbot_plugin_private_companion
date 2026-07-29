from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import types


COMPANION_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = COMPANION_ROOT.parents[1] / "astrbot_plugin_memory_companion-main"


def _load_companion_package():
    name = "c3_dual_companion"
    package = types.ModuleType(name)
    package.__path__ = [str(COMPANION_ROOT)]
    sys.modules[name] = package
    return importlib.import_module(f"{name}.bot_personal_contract"), importlib.import_module(f"{name}.bot_personal_outbox")


def _load_memory_package():
    name = "c3_dual_memory"
    package = types.ModuleType(name)
    package.__path__ = [str(MEMORY_ROOT)]
    sys.modules[name] = package
    core = types.ModuleType(f"{name}.core")
    core.__path__ = [str(MEMORY_ROOT / "core")]
    sys.modules[core.__name__] = core
    bridge = importlib.import_module(f"{name}.core.bridge")
    service = importlib.import_module(f"{name}.core.service")
    store = importlib.import_module(f"{name}.core.store")
    contract = importlib.import_module(f"{name}.core.bot_personal_contract")
    return bridge, service, store, contract


def test_chat_outbox_delivers_structured_archive_to_memory_bridge_without_domain_leak():
    companion_contract, outbox_module = _load_companion_package()
    bridge_module, service_module, store_module, memory_contract = _load_memory_package()
    assert companion_contract.CONTRACT_FINGERPRINT == memory_contract.CONTRACT_FINGERPRINT

    with tempfile.TemporaryDirectory() as temporary:
        service = object.__new__(service_module.MemoryCompanionService)
        service.store = store_module.MemoryStore(Path(temporary) / "memory.db")
        service.store.initialize()
        service._schedule_memory_embedding = lambda *args, **kwargs: None
        bridge = bridge_module.MemoryCompanionBridge(service)
        outbox = outbox_module.BotPersonalOutbox({})

        async def run():
            payload = {
                "date": "2026-07-30",
                "window": "evening",
                "summary": "C3 dual plugin archive",
                "items": ["local first", "bridge second"],
            }
            first = await outbox.enqueue(
                memory_type="bot_schedule_plan",
                payload=payload,
                idempotency_key="daily_plan:2026-07-30",
                occurred_at="2026-07-30T19:00:00+08:00",
                sender=bridge.record_bot_personal_archive,
            )
            duplicate = await outbox.enqueue(
                memory_type="bot_schedule_plan",
                payload=payload,
                idempotency_key="daily_plan:2026-07-30",
                occurred_at="2026-07-30T19:00:00+08:00",
                sender=bridge.record_bot_personal_archive,
            )
            profile = await service.read_bot_personal_profile(limit=10)
            return first, duplicate, profile

        try:
            first, duplicate, profile = asyncio.run(run())
            assert first["ok"] and first["state"] == "sent"
            assert duplicate["deduplicated"] is True
            assert profile["read_only"] is True
            assert len(profile["items"]) == 1
            assert all("payload" not in item and "content" not in item for item in profile["items"])
        finally:
            service.store.close()

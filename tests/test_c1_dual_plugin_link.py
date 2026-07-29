from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PEIBAN_ROOT = ROOT.parents[1]
MEMORY_ROOT = PEIBAN_ROOT / "astrbot_plugin_memory_companion-main"

if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None)
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


def _load_companion_adapter():
    package_name = "c1_dual_companion"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.memory_companion_adapter",
        ROOT / "memory_companion_adapter.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MemoryCompanionAdapterMixin


def _load_memory_bridge():
    package = types.ModuleType("c1_dual_memory")
    package.__path__ = [str(MEMORY_ROOT)]
    sys.modules[package.__name__] = package
    core = types.ModuleType("c1_dual_memory.core")
    core.__path__ = [str(MEMORY_ROOT / "core")]
    sys.modules[core.__name__] = core
    spec = importlib.util.spec_from_file_location(
        "c1_dual_memory.core.bridge",
        MEMORY_ROOT / "core" / "bridge.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MemoryCompanionBridge


MemoryCompanionAdapterMixin = _load_companion_adapter()
MemoryCompanionBridge = _load_memory_bridge()


class _Companion(MemoryCompanionAdapterMixin):
    def __init__(self, bridge):
        self.enable_memory_companion_bridge = True
        self.bridge = bridge

    def _memory_companion_bridge_uncached(self):
        return self.bridge


def test_chat_companion_accepts_chat_memory_capability_contract():
    bridge = MemoryCompanionBridge(
        SimpleNamespace(companion_coordination_status=lambda: {"available": True, "state": "ready"})
    )
    companion = _Companion(bridge)

    assert companion._memory_companion_bridge() is bridge
    probe = bridge.probe_bot_personal_memory_capabilities()
    assert companion._bridge_last_status["contract_fingerprint"] == probe["contract_fingerprint"]
    status = companion._memory_companion_coordination_status()
    assert status["available"] is True
    assert status["state"] == "ready"
    assert probe["available"] is True

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None)
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


def _load_adapter():
    package_name = "c1_private_companion"
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


MemoryCompanionAdapterMixin = _load_adapter()


def _load_contract():
    spec = importlib.util.spec_from_file_location("c1_companion_contract", ROOT / "bot_personal_contract.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = _load_contract()


class _ProbeBridge:
    def probe_bot_personal_memory_capabilities(self):
        result = CONTRACT.capability_descriptor()
        result.update({"available": True, "state": "ready", "degraded": False})
        return result


class _Plugin(MemoryCompanionAdapterMixin):
    def __init__(self, bridge=True, livingmemory=True, bridge_object=None):
        self.enable_memory_companion_bridge = bridge
        self.enable_livingmemory_integration = livingmemory
        self.bridge_object = _ProbeBridge() if bridge_object is True else bridge_object

    def _memory_companion_bridge_uncached(self):
        return self.bridge_object


def test_bridge_and_livingmemory_switches_are_orthogonal():
    bridge = _ProbeBridge()
    assert _Plugin(True, True, bridge)._memory_companion_bridge() is bridge
    assert _Plugin(True, False, bridge)._memory_companion_bridge() is bridge
    assert _Plugin(False, True, bridge)._memory_companion_bridge() is None
    assert _Plugin(False, False, bridge)._memory_companion_bridge() is None


def test_missing_bridge_is_observable_local_only():
    plugin = _Plugin(True, False, None)
    status = plugin._memory_companion_coordination_status()
    assert status["available"] is False
    assert status["state"] == "degraded"
    assert status["reason"] == "bridge_missing"


def test_missing_or_mismatched_capability_probe_is_degraded_without_remote_use():
    missing = _Plugin(True, False, object())
    assert missing._memory_companion_bridge() is None
    assert missing._memory_companion_coordination_status()["reason"] == "capability_probe_missing"

    class _MismatchedBridge(_ProbeBridge):
        def probe_bot_personal_memory_capabilities(self):
            result = super().probe_bot_personal_memory_capabilities()
            result["contract_fingerprint"] = "wrong"
            return result

    mismatched = _Plugin(True, False, _MismatchedBridge())
    assert mismatched._memory_companion_bridge() is None
    status = mismatched._memory_companion_coordination_status()
    assert status["reason"] == "capability_contract_mismatch"
    assert "contract_fingerprint" in status["mismatches"]


def test_prefixed_or_livingmemory_modules_do_not_drive_bridge(monkeypatch):
    module = types.ModuleType("third_party_livingmemory_prefix")
    module.PLUGIN_NAME = "astrbot_plugin_memory_companion_extra"
    module.get_active_bridge = lambda: object()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    plugin = _Plugin(True, False, None)
    assert MemoryCompanionAdapterMixin._memory_companion_bridge_uncached(plugin) is None


def test_legacy_livingmemory_migration_entrypoint_remains():
    assert (ROOT / "integration_status.py").exists()

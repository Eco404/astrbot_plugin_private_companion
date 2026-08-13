from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from astrbot_plugin_private_companion.image_companion_bridge import ImageCompanionBridgeMixin
from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime


class _BridgeHarness(ImageCompanionBridgeMixin):
    context = None


@pytest.mark.asyncio
async def test_image_companion_bridge_preserves_legacy_result_shape() -> None:
    received: dict[str, object] = {}

    class Api:
        async def generate_for_companion(self, owner, request):
            received.update(request)
            assert isinstance(owner, _BridgeHarness)
            return {"handled": True, "backend": "独立后端", "image_path": "C:/output.png", "note": "ok"}

    module_name = "astrbot_plugin_image_companion.main"
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = SimpleNamespace(get_image_companion_api=lambda: Api())
    try:
        result = await _BridgeHarness()._image_companion_generate(
            workflow_kind="selfie",
            prompt_text="take a picture",
            session_key="test",
        )
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous

    assert result == ("独立后端", "C:/output.png", "ok")
    assert received["workflow_kind"] == "selfie"


@pytest.mark.asyncio
async def test_split_runtime_does_not_call_owner_legacy_executor(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Service:
        data_dir = "C:/image-companion"
        image_data_lock = __import__("asyncio").Lock()

        @staticmethod
        def image_setting(_name, default):
            return default

        @staticmethod
        def image_data_for(_owner):
            return {}

    class Owner:
        context = None
        data_dir = "C:/private-companion"

        async def _generate_photo_image_legacy(self, **_kwargs):
            raise AssertionError("split runtime must not call the owner's legacy executor")

    async def split_executor(self, **kwargs):
        calls.append(kwargs)
        return "独立后端", "C:/output.png", "ok"

    monkeypatch.setattr(ImageGenerationRuntime, "_generate_photo_image_legacy", split_executor)
    runtime = ImageGenerationRuntime(Service(), Owner())
    assert await runtime.generate({"workflow_kind": "selfie", "prompt_text": "test", "session_key": "umo"}) == (
        "独立后端",
        "C:/output.png",
        "ok",
    )
    assert calls == [{"workflow_kind": "selfie", "prompt_text": "test", "session_key": "umo"}]


@pytest.mark.asyncio
async def test_production_image_bridge_returns_external_plugin_message_without_fallback() -> None:
    class PrivateCompanionPlugin(ImageCompanionBridgeMixin):
        context = None

        async def _generate_photo_image_legacy(self, **_kwargs):
            raise AssertionError("production host must not invoke the local image executor")

    host = PrivateCompanionPlugin()
    result = await host._image_companion_generate(workflow_kind="selfie", prompt_text="test")
    assert result[0] == "独立生图服务"
    assert "astrbot_plugin_image_companion" in result[2]


def test_image_companion_status_is_unavailable_without_external_plugin() -> None:
    harness = _BridgeHarness()
    harness._image_companion_api = lambda: None

    status = harness._image_companion_status()

    assert status["installed"] is False
    assert status["available"] is False
    assert harness._image_companion_available() is False
    assert harness._image_companion_backend_available("external") is False


@pytest.mark.asyncio
async def test_image_companion_status_and_maintenance_delegate_to_external_api() -> None:
    calls: list[object] = []

    class Api:
        def capability_status(self, owner):
            calls.append(owner)
            return {
                "installed": True,
                "enabled": True,
                "available": True,
                "backends": {"external": True},
            }

        async def maintenance(self, owner):
            calls.append(("maintenance", owner))
            return {"removed_files": 2}

    harness = _BridgeHarness()
    harness._image_companion_api = lambda: Api()

    assert harness._image_companion_available() is True
    assert harness._image_companion_backend_available("external") is True
    assert await harness._image_companion_maintenance() == {"removed_files": 2}
    assert calls[-1] == ("maintenance", harness)

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_private_companion"


class _Logger:
    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __call__(self, *args, **kwargs):
        return _Dummy()

    def __getattr__(self, _name: str):
        return _Dummy()


def _astrbot_stubs() -> dict[str, types.ModuleType]:
    names = (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.provider",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.message.components",
        "astrbot.core.astr_main_agent",
        "astrbot.core.agent",
        "astrbot.core.agent.message",
        "astrbot.core.db",
        "astrbot.core.db.po",
        "astrbot.core.platform",
        "astrbot.core.platform.astrbot_message",
        "astrbot.core.platform.message_session",
        "astrbot.core.platform.message_type",
        "astrbot.core.platform.platform",
        "astrbot.core.platform.platform_metadata",
        "astrbot.core.star",
        "astrbot.core.star.star_handler",
        "astrbot.core.provider",
        "astrbot.core.provider.entities",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    )
    modules = {name: types.ModuleType(name) for name in names}
    for module in modules.values():
        module.__path__ = []

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = _Logger()
    modules["astrbot.api.event"].AstrMessageEvent = _Dummy
    modules["astrbot.api.event"].MessageChain = _Dummy
    modules["astrbot.api.event"].filter = _Dummy()
    for name in ("At", "Image", "Plain", "Record", "Reply"):
        setattr(modules["astrbot.api.message_components"], name, _Dummy)
    modules["astrbot.api.provider"].ProviderRequest = _Dummy
    for name in ("Context", "Star", "StarTools", "register"):
        setattr(modules["astrbot.api.star"], name, _Dummy)
    modules["astrbot.core"].file_token_service = _Dummy()
    for name in ("MainAgentBuildConfig", "build_main_agent"):
        setattr(modules["astrbot.core.astr_main_agent"], name, _Dummy)
    for name in ("AssistantMessageSegment", "TextPart", "UserMessageSegment"):
        setattr(modules["astrbot.core.agent.message"], name, _Dummy)
    modules["astrbot.core.db.po"].Conversation = _Dummy
    symbol_groups = {
        "astrbot.core.platform.astrbot_message": ("AstrBotMessage", "MessageMember"),
        "astrbot.core.platform.message_session": ("MessageSession",),
        "astrbot.core.platform.message_type": ("MessageType",),
        "astrbot.core.platform.platform": ("PlatformStatus",),
        "astrbot.core.platform.platform_metadata": ("PlatformMetadata",),
        "astrbot.core.star.star_handler": ("EventType", "star_handlers_registry"),
        "astrbot.core.provider.entities": ("LLMResponse",),
    }
    for module_name, symbols in symbol_groups.items():
        for symbol in symbols:
            setattr(modules[module_name], symbol, _Dummy)
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = tempfile.gettempdir
    return modules


if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    package.__package__ = PACKAGE_NAME
    sys.modules[PACKAGE_NAME] = package

with mock.patch.dict(sys.modules, _astrbot_stubs()):
    from astrbot_plugin_private_companion.photo_prompt_context import PhotoPromptSection
    from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _PhotoGenerationHarness(ProactiveMessageMixin):
    def __init__(self, output_path: str) -> None:
        self.data: dict = {}
        self.photo_generation_backend = "comfyui"
        self.photo_generation_prompt_format = "traditional"
        self.photo_generation_fixed_prompt = "pajamas; fine film grain"
        self.photo_generation_scene_presets = ""
        self.comfyui_selfie_workflow_name = "selfie-workflow"
        self.comfyui_text2img_workflow_name = ""
        self.output_path = output_path
        self.backend_calls: list[dict[str, str]] = []
        self.dialogue_scene_hint = "Identity: Alice; Today's outfit: blue pajamas; Current location: classroom"

    def _photo_generation_selfie_schedule_scene_hint(self, _user_id: str = "") -> str:
        return self.dialogue_scene_hint

    @staticmethod
    async def _photo_reference_candidate_for_path_async(
        reference_image_path: str,
        **_kwargs,
    ) -> dict:
        return {
            "id": "sleepwear-selfie",
            "path": reference_image_path,
            "source": reference_image_path,
            "kind": "library",
            "reference_roles": ["identity", "outfit"],
            "outfit_category": "sleepwear",
            "outfit_lock_default": True,
        }

    @staticmethod
    def _photo_generation_scene_presets() -> dict[str, str]:
        return {"conflicting preset": "cozy pajamas portrait; warm window light"}

    @staticmethod
    def _apply_photo_generation_scene_presets(
        _prompt_text: str,
        _workflow_kind: str,
        *,
        preset_names: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        return "Scene preset: cozy pajamas portrait; warm window light", list(
            preset_names or ["conflicting preset"]
        )

    @staticmethod
    def _photo_generation_recent_continuity_constraint(
        _workflow_kind: str,
        **_kwargs,
    ) -> tuple[str, bool]:
        return (
            "Recent-photo continuity: preserve identity, face, hairstyle, "
            "and the exact pajamas outfit and accessories.",
            True,
        )

    @staticmethod
    def _write_photo_prompt_debug_file(**_kwargs) -> tuple[str, str]:
        return "", "test-prompt-hash"

    @staticmethod
    def _photo_generation_backend_config_summary() -> str:
        return "test-backend"

    @staticmethod
    def _comfyui_photo_available() -> bool:
        return True

    @staticmethod
    def _local_photo_generation_busy_state(*, force_refresh: bool = False):
        return None

    async def _run_comfyui_photo_workflow(
        self,
        workflow_name: str,
        prompt_text: str,
        *,
        session_key: str,
        reference_image_path: str = "",
    ) -> tuple[str, str]:
        self.backend_calls.append(
            {
                "workflow": workflow_name,
                "prompt": prompt_text,
                "session": session_key,
                "reference": reference_image_path,
            }
        )
        return self.output_path, "generated"

    def _save_data_sync(self) -> None:
        pass


class PhotoPromptContextGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_receives_physically_sanitized_context_and_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "sleepwear-selfie.png"
            output = root / "generated.png"
            reference.write_bytes(b"reference")
            output.write_bytes(b"generated")
            harness = _PhotoGenerationHarness(str(output))

            backend, image_path, _note = await harness._generate_photo_image(
                workflow_kind="selfie",
                prompt_text="Please draw me wearing a school uniform.",
                session_key="test-session",
                continuity_key="test-continuity",
                reference_image_path=str(reference),
                prompt_sections=(
                    PhotoPromptSection(
                        name="user_request",
                        source="user_request",
                        positive="Please draw me wearing a school uniform.",
                        protected=True,
                    ),
                    PhotoPromptSection(
                        name="input_scene",
                        source="scene_context",
                        positive=(
                            "Identity: Alice; Today's outfit: blue pajamas; "
                            "Current location: classroom"
                        ),
                    ),
                    PhotoPromptSection(
                        name="duplicate_context",
                        source="scene_context",
                        positive="first neutral detail",
                    ),
                    PhotoPromptSection(
                        name="duplicate_context",
                        source="scene_context",
                        positive="second neutral detail",
                    ),
                ),
            )

        self.assertEqual(backend, "ComfyUI")
        self.assertEqual(image_path, str(output))
        self.assertEqual(len(harness.backend_calls), 1)
        submitted = harness.backend_calls[0]
        submitted_prompt = submitted["prompt"]
        self.assertEqual(submitted["reference"], "")
        self.assertIn("school uniform", submitted_prompt.lower())
        self.assertIn("current location: classroom", submitted_prompt.lower())
        self.assertIn("warm window light", submitted_prompt.lower())
        self.assertIn("fine film grain", submitted_prompt.lower())
        self.assertNotIn("pajama", submitted_prompt.lower())
        self.assertNotIn("sleepwear", submitted_prompt.lower())
        self.assertNotIn("exact outfit and accessories", submitted_prompt.lower())
        self.assertNotIn("Conflict resolution", submitted_prompt)

        recorded = harness.data["recent_photo_generations"][0]
        self.assertEqual(recorded["residual_conflicts"], [])
        self.assertTrue(recorded["reference_removed"])
        self.assertEqual(recorded["reference_removal"]["rule"], "reference_outfit_conflict")
        self.assertTrue(recorded["detected_conflicts"])
        self.assertTrue(recorded["removed_conflict_details"])
        self.assertEqual(recorded["residual_conflict_details"], [])
        self.assertTrue(
            all(item.get("sha256") for item in recorded["removed_conflict_details"])
        )
        self.assertEqual(recorded["reference_path"], "")
        for section in ("input_scene", "scene_preset", "global_fixed_prompt"):
            self.assertNotIn("pajama", recorded["prompt_sections"][section].lower())
        self.assertEqual(recorded["prompt_sections"]["duplicate_context"], "first neutral detail")
        self.assertEqual(recorded["prompt_sections"]["duplicate_context#2"], "second neutral detail")
        self.assertNotIn("recent_continuity", recorded["prompt_sections"])

    async def test_textual_dialogue_outfit_beats_daily_outfit_on_continue_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "daily-outfit.png"
            output = root / "generated.png"
            reference.write_bytes(b"reference")
            output.write_bytes(b"generated")
            harness = _PhotoGenerationHarness(str(output))
            harness.dialogue_scene_hint = "时间：下午；对话最新服装：换一套JK校服；当天基础穿搭：白衬衫"

            backend, image_path, _note = await harness._generate_photo_image(
                workflow_kind="selfie",
                prompt_text="继续拍一张",
                request_text="继续拍一张",
                requester_user_id="10001",
                requester_is_private=True,
                session_key="dialogue-outfit",
                reference_image_path=str(reference),
            )

        self.assertEqual("ComfyUI", backend)
        self.assertEqual(str(output), image_path)
        submitted_prompt = harness.backend_calls[0]["prompt"].lower()
        self.assertIn("school uniform", submitted_prompt)
        self.assertNotIn("pajama", submitted_prompt)
        self.assertEqual("", harness.backend_calls[0]["reference"])
        record = harness.data["recent_photo_generations"][0]
        self.assertTrue(record["daily_outfit_removed"])


if __name__ == "__main__":
    unittest.main()

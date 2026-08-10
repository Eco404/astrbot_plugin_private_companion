# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
import unittest
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _ReferenceFailureHarness(ProactiveMessageMixin):
    external_image_api_key = "test-key"
    external_image_api_model = "test-image-model"
    external_image_api_size = "1024x1024"
    external_image_api_timeout_seconds = 30

    def _bailian_multimodal_endpoint(self) -> str:
        return "https://example.test/api/v1/services/aigc/multimodal-generation/generation"

    def _gemini_generate_content_endpoint(self) -> str:
        return "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent"

    async def _reference_image_to_data_url(self, _path: str) -> str:
        return ""


class _FakeGeminiResponse:
    status = 403

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def text(self) -> str:
        return '{"error":"test rejection"}'


class _FakeGeminiSession:
    created: list["_FakeGeminiSession"] = []

    def __init__(self, **kwargs) -> None:
        self.session_options = dict(kwargs)
        self.post_endpoint = ""
        self.post_options: dict = {}
        self.__class__.created.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def post(self, endpoint: str, **kwargs):
        self.post_endpoint = endpoint
        self.post_options = dict(kwargs)
        return _FakeGeminiResponse()


class _ComfyHarness(ProactiveMessageMixin):
    comfyui_photo_wait_seconds = 5

    def __init__(self, module) -> None:
        self.module = module

    def _get_comfyui_module(self):
        return self.module


class _GenerationHarness(ProactiveMessageMixin):
    def __init__(self, root: Path, *, generated_path: str, backend: str = "external") -> None:
        self.data_dir = str(root)
        self.data: dict = {}
        self.config: dict = {}
        self.photo_generation_backend = backend
        self.photo_generation_prompt_format = "traditional"
        self.photo_generation_scene_presets: list = []
        self.photo_generation_fixed_prompt = ""
        self.natural_language_photo_extra_prompt = ""
        self.generated_path = generated_path

    def _save_data_sync(self) -> None:
        return None

    def _photo_generation_selfie_schedule_scene_hint(self) -> str:
        return ""

    def _get_photo_style_instruction(self):
        return "default", ""

    async def _select_photo_reference_candidate_async(self, *_args, **_kwargs):
        return {}

    def _photo_generation_backend_config_summary(self) -> str:
        return "test-backend"

    def _external_photo_available(self) -> bool:
        return True

    def _comfyui_photo_available(self) -> bool:
        return False

    def _sdgen_photo_available(self) -> bool:
        return False

    async def _run_external_photo_generation(self, *_args, **_kwargs):
        return self.generated_path, "backend completed"


class _GptImageReferenceHarness(ProactiveMessageMixin):
    photo_generation_backend = "external"
    external_image_api_platform = "openai"
    external_image_api_model = "gpt-image-2"

    def _normalize_external_image_api_platform(self, value: str) -> str:
        return str(value or "auto").strip().lower()

    def _external_photo_available(self) -> bool:
        return True


class _FakeWorkflow:
    latest = None

    def __init__(self, *_args) -> None:
        self.loaded = ""
        self.input_images: list[str] = []
        self.texts: list[str] = []
        _FakeWorkflow.latest = self

    def load_workflow_api(self, workflow_file: str) -> None:
        self.loaded = workflow_file

    async def submit_only(self, input_images, _texts, _videos, *, debug=False):
        self.input_images = list(input_images)
        self.texts = list(_texts)
        return "prompt-1"


class PhotoBackendReferenceContractTests(unittest.IsolatedAsyncioTestCase):
    def test_gpt_image_two_keeps_multi_reference_capacity_and_multipart_fields(self) -> None:
        harness = _GptImageReferenceHarness()

        self.assertEqual(
            harness._photo_reference_backend_max_images(
                "selfie",
                requested_images=4,
            ),
            4,
        )
        harness.external_image_api_model = "gpt-image-1"
        self.assertEqual(
            harness._photo_reference_backend_max_images(
                "selfie",
                requested_images=4,
            ),
            1,
        )
        source = inspect.getsource(
            ProactiveMessageMixin._run_external_photo_edit_generation
        )
        self.assertIn('"image[]" if multi else "image"', source)
        self.assertIn("reference_image_paths", source)

    def test_queue_snapshot_keeps_gpt_image_two_capacity_during_runtime_drift(self) -> None:
        harness = _GptImageReferenceHarness()
        harness.external_image_api_model = "gpt-image-1"
        harness.external_image_api_endpoints = [
            {
                "enabled": False,
                "platform": "openai",
                "base_url": "https://disabled.example/v1",
                "api_key": "disabled",
                "model": "gpt-image-2",
            },
            {
                "enabled": True,
                "platform": "openai",
                "base_url": "https://ready.example/v1",
                "api_key": "ready",
                "model": "gptimage2",
            },
        ]

        self.assertEqual(
            harness._photo_reference_backend_max_images(
                "selfie",
                requested_images=4,
            ),
            4,
        )

        harness.external_image_api_endpoints = [
            {
                "enabled": True,
                "platform": "openai",
                "base_url": "https://single.example/v1",
                "api_key": "single",
                "model": "gpt-image-1",
            }
        ]
        harness.external_image_api_model = "gpt-image-2"
        self.assertEqual(
            harness._photo_reference_backend_max_images(
                "selfie",
                requested_images=4,
            ),
            1,
        )

    async def test_single_image_endpoint_projects_multi_reference_prompt(self) -> None:
        harness = _GptImageReferenceHarness()
        captured: dict[str, object] = {}

        async def run_once(prompt_text: str, **kwargs):
            captured["prompt"] = prompt_text
            captured.update(kwargs)
            return "C:/generated.png", "ok；已使用本地人设参考图"

        harness._run_external_photo_generation_once = run_once
        endpoint = {
            "enabled": True,
            "platform": "openai",
            "base_url": "https://single.example/v1",
            "api_key": "single",
            "model": "gpt-image-1",
        }
        path, note = await harness._run_external_photo_generation_with_endpoint(
            endpoint,
            "reference image 1 is Bot; reference image 2 is sister",
            session_key="single-fallback",
            reference_image_paths=("C:/bot.png", "C:/sister.png"),
        )

        self.assertEqual(path, "C:/generated.png")
        self.assertIn("本地人设参考图", note)
        self.assertIn("实际提交 1/2 张参考图", note)
        self.assertEqual(captured["reference_image_path"], "C:/bot.png")
        self.assertEqual(captured["reference_image_paths"], ("C:/bot.png",))
        self.assertIn("planned reference image(s) 2", captured["prompt"])
        self.assertIn("do not claim an exact visual match", captured["prompt"])

    async def test_bailian_reference_conversion_failure_does_not_submit_text_only(self) -> None:
        harness = _ReferenceFailureHarness()

        path, note = await harness._run_bailian_multimodal_photo_generation(
            "keep the character",
            session_key="bailian-reference-failure",
            reference_image_path="C:/reference/persona.png",
        )

        self.assertEqual(path, "")
        self.assertIn("停止纯文生图回退", note)

    def test_bailian_wan_image_models_use_required_multimodal_protocol(self) -> None:
        harness = _ReferenceFailureHarness()
        for model in ("wan2.7-image", "wan2.6-image-edit"):
            harness.external_image_api_model = model
            self.assertTrue(harness._bailian_prefers_multimodal(), model)
            self.assertTrue(harness._bailian_requires_multimodal(), model)

        harness.external_image_api_model = "qwen-image-plus"
        self.assertTrue(harness._bailian_prefers_multimodal())
        self.assertFalse(harness._bailian_requires_multimodal())

        harness.external_image_api_model = "qwen-image-edit"
        self.assertTrue(harness._bailian_prefers_multimodal())
        self.assertFalse(harness._bailian_requires_multimodal())

        harness.external_image_api_model = "wan-video"
        self.assertFalse(harness._bailian_prefers_multimodal())
        self.assertFalse(harness._bailian_requires_multimodal())

    async def test_gemini_reference_conversion_failure_does_not_submit_text_only(self) -> None:
        harness = _ReferenceFailureHarness()

        with patch(
            "astrbot_plugin_private_companion.proactive_message.os.path.exists",
            return_value=True,
        ):
            path, note = await harness._run_gemini_photo_generation(
                "keep the character",
                session_key="gemini-reference-failure",
                reference_image_path="C:/reference/persona.png",
            )

        self.assertEqual(path, "")
        self.assertIn("停止纯文生图回退", note)

    async def test_gemini_generation_uses_explicit_and_environment_proxy_settings(self) -> None:
        harness = _ReferenceFailureHarness()
        harness.external_image_download_proxy = "http://127.0.0.1:7897"
        harness.external_image_download_use_environment_proxy = True
        _FakeGeminiSession.created = []
        fake_aiohttp = SimpleNamespace(
            ClientSession=_FakeGeminiSession,
            ClientTimeout=lambda **kwargs: SimpleNamespace(**kwargs),
        )

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            path, note = await harness._run_gemini_photo_generation(
                "draw a quiet room",
                session_key="gemini-proxy",
            )

        self.assertEqual("", path)
        self.assertIn("HTTP 403", note)
        self.assertEqual(1, len(_FakeGeminiSession.created))
        session = _FakeGeminiSession.created[0]
        self.assertTrue(session.session_options["trust_env"])
        self.assertEqual("http://127.0.0.1:7897", session.post_options["proxy"])
        self.assertIn("generativelanguage.googleapis.com", session.post_endpoint)

    async def test_gemini_generation_can_use_environment_proxy_without_explicit_proxy(self) -> None:
        harness = _ReferenceFailureHarness()
        harness.external_image_download_proxy = ""
        harness.external_image_download_use_environment_proxy = True
        _FakeGeminiSession.created = []
        fake_aiohttp = SimpleNamespace(
            ClientSession=_FakeGeminiSession,
            ClientTimeout=lambda **kwargs: SimpleNamespace(**kwargs),
        )

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            await harness._run_gemini_photo_generation(
                "draw a quiet room",
                session_key="gemini-environment-proxy",
            )

        session = _FakeGeminiSession.created[0]
        self.assertTrue(session.session_options["trust_env"])
        self.assertNotIn("proxy", session.post_options)

    async def test_comfyui_preserves_long_reference_path_in_input_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "generated.png"
            generated.write_bytes(b"generated")
            long_reference = "C:/reference/" + ("nested folder/" * 22) + "persona  original.png"

            def find_workflow_file(_name, _texts, images, _videos, _directory):
                return "workflow-images-1.json" if images == 1 else "workflow-images-0.json"

            module = SimpleNamespace(
                _plugin_config={"debug_mode": False},
                _get_server_config=lambda _config: ("127.0.0.1:8188", "client"),
                _get_workflow_dir=lambda: directory,
                find_workflow_file=find_workflow_file,
                ComfyUIWorkflow=_FakeWorkflow,
                _get_result_for_prompt=self._comfy_result,
                _download_image_to_temp=self._comfy_download,
                _save_image_to_persistent_path=lambda *_args: None,
            )

            async def save_persistent(_temp_path, _session_key):
                return str(generated)

            module._save_image_to_persistent_path = save_persistent
            harness = _ComfyHarness(module)
            with patch(
                "astrbot_plugin_private_companion.proactive_message.os.path.isfile",
                return_value=True,
            ):
                path, note = await harness._run_comfyui_photo_workflow(
                    "selfie",
                    "keep the character",
                    session_key="comfy-long-path",
                    reference_image_path=long_reference,
                )

        self.assertGreater(len(long_reference), 260)
        self.assertEqual(path, str(generated))
        self.assertIn("已使用 1 张本地参考图", note)
        self.assertEqual(_FakeWorkflow.latest.input_images, [long_reference])

    async def test_comfyui_falls_back_to_available_single_image_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated.png"
            generated.write_bytes(b"generated")
            bot_reference = root / "bot.png"
            role_reference = root / "sister.png"
            bot_reference.write_bytes(b"bot")
            role_reference.write_bytes(b"sister")

            def find_workflow_file(_name, _texts, images, _videos, _directory):
                return "workflow-images-1.json" if images == 1 else ""

            module = SimpleNamespace(
                _plugin_config={"debug_mode": False},
                _get_server_config=lambda _config: ("127.0.0.1:8188", "client"),
                _get_workflow_dir=lambda: directory,
                find_workflow_file=find_workflow_file,
                ComfyUIWorkflow=_FakeWorkflow,
                _get_result_for_prompt=self._comfy_result,
                _download_image_to_temp=self._comfy_download,
            )

            async def save_persistent(_temp_path, _session_key):
                return str(generated)

            module._save_image_to_persistent_path = save_persistent
            harness = _ComfyHarness(module)
            path, note = await harness._run_comfyui_photo_workflow(
                "selfie",
                "reference image 1 is Bot; reference image 2 is sister",
                session_key="comfy-capacity-fallback",
                reference_image_paths=(str(bot_reference), str(role_reference)),
            )

        self.assertEqual(path, str(generated))
        self.assertIn("当前工作流仅支持 1/2 张参考图", note)
        self.assertEqual(_FakeWorkflow.latest.input_images, [str(bot_reference)])
        self.assertIn("planned reference image(s) 2", _FakeWorkflow.latest.texts[0])

    async def test_nonexistent_backend_output_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.png")
            harness = _GenerationHarness(Path(directory), generated_path=missing)

            backend, image_path, note = await harness._generate_photo_image(
                workflow_kind="text2img",
                prompt_text="a desk lamp",
                session_key="missing-output",
            )

        self.assertEqual(backend, "在线图片 API")
        self.assertEqual(image_path, "")
        self.assertIn("图片文件不存在", note)
        self.assertFalse(harness.data["recent_photo_generations"][0]["ok"])

    async def test_auto_chain_failure_uses_chain_backend_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _GenerationHarness(Path(directory), generated_path="", backend="auto")

            backend, image_path, note = await harness._generate_photo_image(
                workflow_kind="text2img",
                prompt_text="a desk lamp",
                session_key="failed-chain",
            )

        self.assertEqual(backend, "生图链路")
        self.assertEqual(image_path, "")
        self.assertIn("在线图片 API 失败", note)

    @staticmethod
    async def _comfy_result(_server_ip, _prompt_id):
        return "https://example.test/generated.png", "image", []

    @staticmethod
    async def _comfy_download(_url):
        return "C:/temp/comfy-generated.png"


if __name__ == "__main__":
    unittest.main()

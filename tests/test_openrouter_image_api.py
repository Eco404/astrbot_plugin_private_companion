# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self) -> str:
        return json.dumps(self.payload)


class _Session:
    def __init__(self, response: _Response, calls: list[dict[str, object]], **_kwargs) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, endpoint: str, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self.response


class _Harness(ProactiveMessageMixin):
    external_image_api_platform = "openai"
    external_image_api_base_url = "https://openrouter.ai/api/v1"
    external_image_api_key = "test-key"
    external_image_api_model = "x-ai/grok-imagine-image-quality"
    external_image_api_size = "1024x1024"
    external_image_api_timeout_seconds = 30
    external_image_api_custom_headers = ""
    external_image_api_endpoints: list[dict[str, object]] = []
    config: dict[str, object] = {}

    @staticmethod
    def _normalize_external_image_api_platform(value):
        return PrivateCompanionPlugin._normalize_external_image_api_platform(value)

    @staticmethod
    def _extract_json_payload(text: str) -> object:
        return json.loads(text)

    async def _save_external_generated_image(
        self,
        image_bytes: bytes,
        *,
        session_key: str,
        ext: str,
    ) -> str:
        self.saved_image = (image_bytes, session_key, ext)
        return "C:/temp/openrouter-result.png"


class OpenRouterImageApiTests(unittest.IsolatedAsyncioTestCase):
    def test_platform_aliases_and_endpoint_normalization_recognize_openrouter(self) -> None:
        for alias in ("openrouter", "OpenRouter", "open-router", "open_router", "openrouter.ai"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    PrivateCompanionPlugin._normalize_external_image_api_platform(alias),
                    "openrouter",
                )

        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        endpoint = plugin._normalize_external_image_api_endpoint(
            {
                "platform": "openai",
                "base_url": "https://openrouter.ai/api/v1/",
                "model": "x-ai/grok-imagine-image-quality",
            }
        )
        self.assertEqual(endpoint["platform"], "openrouter")

    def test_openrouter_base_with_or_without_trailing_slash_uses_images_for_references(self) -> None:
        harness = _Harness()
        for base_url in (
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/",
        ):
            with self.subTest(base_url=base_url):
                harness.external_image_api_base_url = base_url
                self.assertEqual(harness._resolved_external_image_api_platform(), "openrouter")
                self.assertEqual(
                    harness._external_image_endpoint("edits"),
                    "https://openrouter.ai/api/v1/images",
                )
                self.assertEqual(
                    harness._external_image_endpoint("generations"),
                    "https://openrouter.ai/api/v1/images/generations",
                )
                self.assertEqual(
                    harness._external_image_endpoint_candidates("edits"),
                    ["https://openrouter.ai/api/v1/images"],
                )

    def test_openrouter_api_v1_is_not_rewritten_as_dashscope(self) -> None:
        harness = _Harness()
        for base_url in (
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/",
        ):
            with self.subTest(base_url=base_url):
                self.assertEqual(
                    harness._normalized_external_image_api_base_url(
                        base_url,
                        platform="openai",
                    ),
                    base_url.rstrip("/"),
                )
        self.assertFalse(
            harness._external_image_api_is_openrouter_url(
                "https://openrouter.ai.example.test/api/v1"
            )
        )

    def test_openrouter_reference_capacity_is_capped_at_three(self) -> None:
        harness = _Harness()
        endpoint = {
            "platform": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "model": harness.external_image_api_model,
        }
        self.assertEqual(
            harness._external_image_endpoint_multi_reference_capacity(endpoint, 2),
            2,
        )
        self.assertEqual(
            harness._external_image_endpoint_multi_reference_capacity(endpoint, 8),
            3,
        )

    async def test_reference_request_uses_input_references_json_and_parses_b64_response(self) -> None:
        generated = b"\x89PNG\r\n\x1a\nopenrouter-generated"
        response_payload = {
            "data": [
                {
                    "b64_json": base64.b64encode(generated).decode("ascii"),
                    "media_type": "image/png",
                }
            ]
        }

        for base_url in (
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/",
        ):
            with self.subTest(base_url=base_url), tempfile.TemporaryDirectory() as temp_dir:
                harness = _Harness()
                harness.external_image_api_base_url = base_url
                reference = Path(temp_dir) / "reference.png"
                reference_bytes = b"\x89PNG\r\n\x1a\nreference"
                reference.write_bytes(reference_bytes)
                calls: list[dict[str, object]] = []

                def session_factory(**kwargs):
                    return _Session(_Response(response_payload), calls, **kwargs)

                with patch("aiohttp.ClientSession", new=session_factory):
                    path, note = await harness._run_external_photo_generation_once(
                        "keep the same person",
                        session_key="openrouter-reference",
                        reference_image_path=str(reference),
                    )

                self.assertEqual(path, "C:/temp/openrouter-result.png")
                self.assertIn("已使用本地人设参考图", note)
                self.assertEqual(
                    harness.saved_image,
                    (generated, "openrouter-reference", ".png"),
                )
                self.assertEqual(len(calls), 1)
                call = calls[0]
                self.assertEqual(
                    call["endpoint"],
                    "https://openrouter.ai/api/v1/images",
                )
                self.assertNotIn("data", call)
                payload = call["json"]
                self.assertEqual(payload["model"], harness.external_image_api_model)
                self.assertEqual(payload["prompt"], "keep the same person")
                references = payload["input_references"]
                self.assertEqual(len(references), 1)
                self.assertEqual(references[0]["type"], "image_url")
                data_url = references[0]["image_url"]["url"]
                self.assertTrue(data_url.startswith("data:image/png;base64,"))
                self.assertEqual(
                    base64.b64decode(data_url.split(",", 1)[1]),
                    reference_bytes,
                )

    async def test_openrouter_submits_at_most_three_input_references(self) -> None:
        generated = b"\x89PNG\r\n\x1a\nopenrouter-multi"
        calls: list[dict[str, object]] = []

        def session_factory(**kwargs):
            return _Session(
                _Response(
                    {
                        "data": [
                            {"b64_json": base64.b64encode(generated).decode("ascii")}
                        ]
                    }
                ),
                calls,
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            references = []
            for index in range(4):
                reference = Path(temp_dir) / f"reference-{index}.png"
                reference.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
                references.append(str(reference))
            with patch("aiohttp.ClientSession", new=session_factory):
                path, note = await _Harness()._run_external_photo_generation_once(
                    "four planned identities",
                    session_key="openrouter-multi",
                    reference_image_path=references[0],
                    reference_image_paths=tuple(references[1:]),
                )

        self.assertEqual(path, "C:/temp/openrouter-result.png")
        self.assertIn("已使用 3 张参考图", note)
        self.assertIn("仅支持 3/4 张参考图", note)
        payload = calls[0]["json"]
        self.assertEqual(len(payload["input_references"]), 3)
        self.assertIn("planned reference image(s) 4", payload["prompt"])

    async def test_openrouter_text_generation_keeps_generations_endpoint(self) -> None:
        generated = b"\x89PNG\r\n\x1a\nopenrouter-text"
        calls: list[dict[str, object]] = []

        def session_factory(**kwargs):
            return _Session(
                _Response(
                    {
                        "data": [
                            {"b64_json": base64.b64encode(generated).decode("ascii")}
                        ]
                    }
                ),
                calls,
                **kwargs,
            )

        with patch("aiohttp.ClientSession", new=session_factory):
            path, note = await _Harness()._run_external_photo_generation_once(
                "a text-only scene",
                session_key="openrouter-text",
            )

        self.assertEqual(path, "C:/temp/openrouter-result.png")
        self.assertEqual(note, "ok")
        self.assertEqual(
            calls[0]["endpoint"],
            "https://openrouter.ai/api/v1/images/generations",
        )
        self.assertIn("json", calls[0])
        self.assertNotIn("input_references", calls[0]["json"])

    async def test_regular_openai_reference_request_stays_multipart_edits(self) -> None:
        generated = b"\x89PNG\r\n\x1a\nopenai-edit"
        calls: list[dict[str, object]] = []

        def session_factory(**kwargs):
            return _Session(
                _Response(
                    {
                        "data": [
                            {"b64_json": base64.b64encode(generated).decode("ascii")}
                        ]
                    }
                ),
                calls,
                **kwargs,
            )

        harness = _Harness()
        harness.external_image_api_base_url = "https://images.example.test/v1"
        harness.external_image_api_platform = "openai"
        harness.external_image_api_model = "gpt-image-1"
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"\x89PNG\r\n\x1a\nreference")
            with patch("aiohttp.ClientSession", new=session_factory):
                path, note = await harness._run_external_photo_generation_once(
                    "keep the same subject",
                    session_key="openai-edit",
                    reference_image_path=str(reference),
                )

        self.assertEqual(path, "C:/temp/openrouter-result.png")
        self.assertIn("已使用本地人设参考图", note)
        self.assertEqual(
            calls[0]["endpoint"],
            "https://images.example.test/v1/images/edits",
        )
        self.assertIn("data", calls[0])
        self.assertNotIn("json", calls[0])


if __name__ == "__main__":
    unittest.main()

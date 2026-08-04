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


class _MiniMaxHarness(ProactiveMessageMixin):
    external_image_api_platform = "auto"
    external_image_api_base_url = "https://api.minimaxi.com/v1/image/generation"
    external_image_api_key = "test-key"
    external_image_api_model = "image-01"
    external_image_api_size = "1024x1024"
    external_image_api_ratio = ""
    external_image_api_timeout_seconds = 30
    external_image_api_custom_headers = ""
    external_image_download_proxy = ""
    external_image_download_use_environment_proxy = False

    @staticmethod
    def _normalize_external_image_api_platform(value):
        return PrivateCompanionPlugin._normalize_external_image_api_platform(value)

    @staticmethod
    def _extract_json_payload(text):
        return json.loads(text)

    async def _save_external_generated_image(self, image_bytes, *, session_key, ext):
        self.saved_image = (image_bytes, session_key, ext)
        return "C:/temp/minimax-result.png"


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return json.dumps(self.payload)


class _FakeSession:
    def __init__(self, capture, response_payload, **_kwargs):
        self.capture = capture
        self.response_payload = response_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, endpoint, **kwargs):
        self.capture.update({"endpoint": endpoint, **kwargs})
        return _FakeResponse(self.response_payload)


class MiniMaxImageApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = _MiniMaxHarness()

    def test_auto_detects_official_host_and_normalizes_slash_typo(self) -> None:
        self.assertEqual(self.harness._resolved_external_image_api_platform(), "minimax")
        self.assertEqual(
            self.harness._normalized_external_image_api_base_url(platform="minimax"),
            "https://api.minimaxi.com/v1",
        )
        self.assertEqual(
            self.harness._external_image_endpoint(),
            "https://api.minimaxi.com/v1/image_generation",
        )
        candidates = self.harness._external_image_endpoint_candidates()
        self.assertEqual(candidates[0], "https://api.minimaxi.com/v1/image_generation")
        self.assertNotIn("https://api.minimaxi.com/v1/image/generation", candidates)

        for configured_url in (
            "https://api.minimaxi.com",
            "https://api.minimaxi.com/v1",
            "https://api.minimaxi.com/v1/image_generation",
        ):
            with self.subTest(configured_url=configured_url):
                self.harness.external_image_api_base_url = configured_url
                self.assertEqual(
                    self.harness._external_image_endpoint(),
                    "https://api.minimaxi.com/v1/image_generation",
                )

    def test_endpoint_queue_normalizes_full_typo_and_international_host(self) -> None:
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        normalized = plugin._normalize_external_image_api_endpoint(
            {
                "platform": "auto",
                "base_url": "https://api.minimaxi.com/v1/image/generation",
                "model": "image-01",
            }
        )
        self.assertEqual(normalized["platform"], "minimax")
        self.assertEqual(normalized["base_url"], "https://api.minimaxi.com/v1/image_generation")

        self.harness.external_image_api_base_url = "https://api.minimax.io/v1/images/generations"
        self.assertEqual(
            self.harness._external_image_endpoint(),
            "https://api.minimax.io/v1/image_generation",
        )

    def test_explicit_openai_proxy_is_not_overridden_by_minimax_model_name(self) -> None:
        self.harness.external_image_api_platform = "openai"
        self.harness.external_image_api_base_url = "https://image-proxy.example/v1"
        self.harness.external_image_api_model = "image-01"
        self.assertEqual(self.harness._resolved_external_image_api_platform(), "openai")
        self.assertEqual(
            self.harness._external_image_endpoint(),
            "https://image-proxy.example/v1/images/generations",
        )

        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        normalized = plugin._normalize_external_image_api_endpoint(
            {
                "platform": "openai",
                "base_url": "https://image-proxy.example/v1",
                "model": "image-01-live",
            }
        )
        self.assertEqual(normalized["platform"], "openai")

    def test_live_model_maps_dimensions_to_supported_aspect_ratio(self) -> None:
        self.harness.external_image_api_model = "image-01-live"

        self.harness.external_image_api_ratio = "21:9"
        payload = self.harness._minimax_image_size_payload("1344x576")
        self.assertEqual(payload, {"aspect_ratio": "16:9"})

        self.harness.external_image_api_ratio = ""
        payload = self.harness._minimax_image_size_payload("768x1344")
        self.assertEqual(payload, {"aspect_ratio": "9:16"})
        self.assertNotIn("width", payload)
        self.assertNotIn("height", payload)

    async def test_text_generation_posts_minimax_json_and_accepts_image_base64(self) -> None:
        capture = {}
        generated = b"\x89PNG\r\n\x1a\nminimax-base64-result"
        response_payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {"image_base64": [base64.b64encode(generated).decode("ascii")]},
        }

        def session_factory(**kwargs):
            return _FakeSession(capture, response_payload, **kwargs)

        with patch("aiohttp.ClientSession", new=session_factory):
            path, note = await self.harness._run_external_photo_generation_once(
                "a quiet room at sunset",
                session_key="minimax-base64",
            )

        self.assertEqual(path, "C:/temp/minimax-result.png")
        self.assertEqual(note, "ok")
        self.assertEqual(
            self.harness.saved_image,
            (generated, "minimax-base64", ".png"),
        )
        self.assertEqual(capture["endpoint"], "https://api.minimaxi.com/v1/image_generation")
        payload = capture["json"]
        self.assertEqual(payload["model"], "image-01")
        self.assertEqual(payload["prompt"], "a quiet room at sunset")
        if "aspect_ratio" in payload:
            self.assertEqual(payload["aspect_ratio"], "1:1")
        else:
            self.assertEqual(payload["width"], 1024)
            self.assertEqual(payload["height"], 1024)
        self.assertEqual(payload["n"], 1)
        self.assertIn(payload["response_format"], {"url", "base64"})
        self.assertNotIn("size", payload)

    async def test_image_urls_response_is_accepted(self) -> None:
        capture = {}
        response_payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {"image_urls": ["https://cdn.minimaxi.test/generated.png"]},
        }

        def session_factory(**kwargs):
            return _FakeSession(capture, response_payload, **kwargs)

        async def download(url, *, session_key):
            self.harness.downloaded_url = (url, session_key)
            return "C:/temp/minimax-url-result.png", "downloaded"

        self.harness._download_external_image_url = download
        with patch("aiohttp.ClientSession", new=session_factory):
            path, note = await self.harness._run_external_photo_generation_once(
                "a small desk lamp",
                session_key="minimax-url",
            )

        self.assertEqual(path, "C:/temp/minimax-url-result.png")
        self.assertEqual(note, "ok")
        self.assertEqual(
            self.harness.downloaded_url,
            ("https://cdn.minimaxi.test/generated.png", "minimax-url"),
        )

    async def test_reference_image_uses_subject_reference_json(self) -> None:
        capture = {}
        generated = b"\x89PNG\r\n\x1a\nminimax-reference-result"
        response_payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {"image_base64": [base64.b64encode(generated).decode("ascii")]},
        }

        def session_factory(**kwargs):
            return _FakeSession(capture, response_payload, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"reference-image")
            with patch("aiohttp.ClientSession", new=session_factory):
                path, note = await self.harness._run_external_photo_generation_once(
                    "keep the same character in a new scene",
                    session_key="minimax-reference",
                    reference_image_path=str(reference),
                )

        self.assertEqual(path, "C:/temp/minimax-result.png")
        self.assertIn("参考图", note)
        self.assertEqual(capture["endpoint"], "https://api.minimaxi.com/v1/image_generation")
        references = capture["json"]["subject_reference"]
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["type"], "character")
        self.assertTrue(references[0]["image_file"].startswith("data:image/png;base64,"))

    async def test_reference_image_rejects_unsupported_format_and_oversize_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            webp_reference = Path(temp_dir) / "reference.webp"
            webp_reference.write_bytes(b"RIFF-test-WEBP")
            path, note = await self.harness._run_external_photo_generation_once(
                "keep the same character",
                session_key="minimax-webp-reference",
                reference_image_path=str(webp_reference),
            )
            self.assertEqual(path, "")
            self.assertIn("PNG 或 JPEG", note)

            png_reference = Path(temp_dir) / "reference.png"
            png_reference.write_bytes(b"12345678")
            with patch(
                "astrbot_plugin_private_companion.proactive_message._MINIMAX_REFERENCE_IMAGE_MAX_BYTES",
                8,
            ):
                path, note = await self.harness._run_external_photo_generation_once(
                    "keep the same character",
                    session_key="minimax-oversize-reference",
                    reference_image_path=str(png_reference),
                )
            self.assertEqual(path, "")
            self.assertIn("小于 10 MB", note)

    async def test_nonzero_base_resp_is_reported_as_failure(self) -> None:
        capture = {}
        response_payload = {
            "base_resp": {"status_code": 1004, "status_msg": "invalid api key"},
            "data": {"image_urls": []},
        }

        def session_factory(**kwargs):
            return _FakeSession(capture, response_payload, **kwargs)

        with patch("aiohttp.ClientSession", new=session_factory):
            path, note = await self.harness._run_external_photo_generation_once(
                "a small desk lamp",
                session_key="minimax-api-error",
            )

        self.assertEqual(path, "")
        self.assertIn("1004", note)
        self.assertIn("invalid api key", note)

    async def test_business_error_falls_through_to_next_online_endpoint(self) -> None:
        captures = []
        generated = b"\x89PNG\r\n\x1a\nopenai-fallback-result"
        responses = [
            {
                "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"},
                "data": {"image_urls": []},
            },
            {"data": [{"b64_json": base64.b64encode(generated).decode("ascii")}]},
        ]

        def session_factory(**kwargs):
            capture = {}
            captures.append(capture)
            return _FakeSession(capture, responses.pop(0), **kwargs)

        endpoints = [
            {
                "name": "MiniMax 主用",
                "enabled": True,
                "platform": "minimax",
                "base_url": "https://api.minimaxi.com/v1",
                "api_key": "minimax-key",
                "model": "image-01",
                "size": "1024x1024",
                "ratio": "",
                "timeout_seconds": 30,
                "custom_headers": "",
            },
            {
                "name": "OpenAI 备选",
                "enabled": True,
                "platform": "openai",
                "base_url": "https://image-proxy.example/v1",
                "api_key": "openai-key",
                "model": "gpt-image-1",
                "size": "1024x1024",
                "ratio": "",
                "timeout_seconds": 30,
                "custom_headers": "",
            },
        ]
        self.harness._external_image_api_endpoint_queue = lambda **_kwargs: endpoints

        with patch("aiohttp.ClientSession", new=session_factory):
            path, note = await self.harness._run_external_photo_generation_serial(
                "a small desk lamp",
                session_key="minimax-fallback",
            )

        self.assertEqual(path, "C:/temp/minimax-result.png")
        self.assertIn("OpenAI 备选", note)
        self.assertEqual(len(captures), 2)
        self.assertEqual(captures[0]["endpoint"], "https://api.minimaxi.com/v1/image_generation")
        self.assertEqual(captures[1]["endpoint"], "https://image-proxy.example/v1/images/generations")


if __name__ == "__main__":
    unittest.main()

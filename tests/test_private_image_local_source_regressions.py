# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_private_companion.private_image import PrivateImageMixin


class _ImageHarness(PrivateImageMixin):
    def __init__(self, root: Path) -> None:
        self.data_dir = str(root)
        self.context_image_caption_timeout_seconds = 8
        self.private_image_provider_timeout_seconds = 12
        self.private_image_vision_wait_seconds = 30


class PrivateImageLocalSourceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_file_uri_is_normalized_before_model_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "带 空格.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            harness = _ImageHarness(root)
            uri = image.resolve().as_uri()

            prepared = await harness._prepare_private_image_sources_for_model([uri], namespace="private_vision")

            self.assertEqual([str(image.resolve())], prepared)
            self.assertTrue(harness._private_image_source_to_model_url(uri).startswith("data:image/png;base64,"))

    async def test_context_caption_waits_for_visual_budget_not_legacy_eight_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            observed: dict[str, float] = {}

            async def transcribe(_sources, **_kwargs):
                return "图片类型：照片"

            async def fake_wait_for(awaitable, timeout):
                observed["timeout"] = timeout
                return await awaitable

            harness._transcribe_private_inbound_images = transcribe
            with patch("astrbot_plugin_private_companion.private_image.asyncio.wait_for", new=fake_wait_for):
                result = await harness._caption_context_image_sources(["source"])

            self.assertEqual("图片类型：照片", result)
            self.assertEqual(30, observed["timeout"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


ROOT = Path(__file__).resolve().parents[1]


class _PromptFormatHarness(ProactiveMessageMixin):
    photo_generation_prompt_format = "traditional"
    photo_generation_text2img_fixed_prompt = "text fixed"
    photo_generation_selfie_fixed_prompt = "selfie fixed"
    photo_generation_edit_fixed_prompt = "edit fixed"


class PhotoPromptFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _PromptFormatHarness()

    def test_traditional_mode_converts_prose_to_positive_and_negative_prompts(self) -> None:
        self.harness.photo_generation_prompt_format = "traditional"

        prompt = self.harness._apply_photo_generation_prompt_format(
            "A girl reads beside a rainy window. Soft cinematic light. Do not show text or a watermark."
        )

        self.assertTrue(prompt.startswith("Positive prompt:"))
        self.assertIn("A girl reads beside a rainy window", prompt)
        self.assertIn("Negative prompt:", prompt)
        self.assertIn("show text or a watermark", prompt)

    def test_natural_language_mode_converts_structured_tag_prompt(self) -> None:
        self.harness.photo_generation_prompt_format = "natural_language"

        prompt = self.harness._apply_photo_generation_prompt_format(
            "Positive prompt: solo girl, rainy window, soft light. Negative prompt: text, watermark, extra people."
        )

        self.assertTrue(prompt.startswith("Create a single coherent image showing"))
        self.assertNotIn("Positive prompt:", prompt)
        self.assertNotIn("Negative prompt:", prompt)
        self.assertIn("Avoid text, watermark, extra people", prompt)

    def test_nai_mode_preserves_inline_weights_and_converts_negative_prompt(self) -> None:
        self.harness.photo_generation_prompt_format = "nai"

        prompt = self.harness._apply_photo_generation_prompt_format(
            "Positive prompt: {1.5::solo girl, rainy window::}, soft light. Negative prompt: text, watermark."
        )

        self.assertNotIn("Positive prompt:", prompt)
        self.assertNotIn("Negative prompt:", prompt)
        self.assertIn("{1.5::solo girl, rainy window::}", prompt)
        self.assertIn("-1.5::text, watermark::", prompt)

        instruction = self.harness._photo_generation_prompt_format_instruction()
        self.assertIn("NAI", instruction)
        self.assertIn("权重", instruction)

    def test_nai_mode_converts_standalone_negative_prompt(self) -> None:
        self.harness.photo_generation_prompt_format = "nai"

        prompt = self.harness._apply_photo_generation_prompt_format(
            "Negative prompt: text, watermark."
        )

        self.assertEqual(prompt, "-1.5::text, watermark::")

    def test_relationship_cards_are_normalized_deduplicated_and_limited(self) -> None:
        cards = [
            " || 没有名字 || 应丢弃",
            "小林 || 朋友 || 黑框眼镜",
            "小林 || 重复 || 不应保留",
            *(f"角色{i} || 朋友 || 外貌{i}" for i in range(20)),
        ]

        normalized = self.harness._normalize_bot_relationship_cards(cards)

        self.assertEqual(normalized[0], "小林 || 朋友 || 黑框眼镜")
        self.assertEqual(len(normalized), 16)
        self.assertEqual(sum(card.startswith("小林 ||") for card in normalized), 1)

    def test_prompt_model_instruction_matches_selected_format(self) -> None:
        self.harness.photo_generation_prompt_format = "traditional"
        self.assertIn("Positive prompt", self.harness._photo_generation_prompt_format_instruction())
        self.harness.photo_generation_prompt_format = "natural_language"
        instruction = self.harness._photo_generation_prompt_format_instruction()
        self.assertIn("自然语言描述", instruction)
        self.assertIn("连贯", instruction)

    def test_all_generation_backends_receive_the_central_format_pass(self) -> None:
        source = inspect.getsource(ProactiveMessageMixin._generate_photo_image)
        format_index = source.index("_apply_photo_generation_prompt_format")
        preset_index = source.index("_apply_photo_generation_scene_presets")
        self.assertLess(format_index, preset_index)

    def test_config_schema_page_and_save_normalization_expose_all_modes(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        item = schema["photo_action_config"]["items"]["photo_generation_prompt_format"]
        self.assertEqual(item["default"], "traditional")
        self.assertEqual(item["options"], ["traditional", "natural_language", "nai"])

        api = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
        api.plugin = self.harness
        api._schema_key_index_cache = None
        self.assertEqual(api._normalize_setting_value("photo_generation_prompt_format", "自然语言"), "natural_language")
        self.assertEqual(api._normalize_setting_value("photo_generation_prompt_format", "NAI"), "nai")
        self.assertEqual(api._normalize_setting_value("photo_generation_prompt_format", "invalid"), "traditional")
        self.assertIn("photo_generation_prompt_format", api._allowed_setting_keys())

        fallback_api = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
        fallback_api.plugin = object()
        fallback_api._schema_key_index_cache = None
        self.assertEqual(
            fallback_api._normalize_setting_value(
                "bot_relationship_cards",
                "小林 || 朋友 || 黑框眼镜\n小林 || 重复 || 不保留",
            ),
            ["小林 || 朋友 || 黑框眼镜"],
        )

        script = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        self.assertIn('photo_generation_prompt_format: { type: "select"', script)
        self.assertIn('"自然语言描述"', script)
        self.assertIn('"传统文生图提示词（标签/短语）"', script)
        self.assertIn('"NAI 联动模式（NovelAI 标签语法）"', script)
        self.assertIn('data-relationship-card-count aria-live="polite"', script)
        self.assertIn('addButton.disabled = cards.length >= RELATIONSHIP_CARD_MAX', script)
        self.assertIn('const seenNames = new Set()', script)
        self.assertIn('maxlength="200" data-relationship-card-field="name"', script)

    def test_workflow_fixed_prompt_config_is_exposed_and_normalized(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["photo_action_config"]["items"]
        keys = (
            "photo_generation_text2img_fixed_prompt",
            "photo_generation_selfie_fixed_prompt",
            "photo_generation_edit_fixed_prompt",
        )
        api = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
        api.plugin = self.harness
        api._schema_key_index_cache = None
        allowed = api._allowed_setting_keys()

        for key in keys:
            self.assertEqual(items[key]["default"], "")
            self.assertEqual(schema[key]["default"], "")
            self.assertTrue(schema[key]["invisible"])
            self.assertIn(key, allowed)
            self.assertEqual(api._normalize_setting_value(key, "  fixed prompt  "), "fixed prompt")

        expected = {
            "text2img": ("text2img", "photo_generation_text2img_fixed_prompt", "text fixed"),
            " TEXT2IMG ": ("text2img", "photo_generation_text2img_fixed_prompt", "text fixed"),
            "unknown-workflow": ("text2img", "photo_generation_text2img_fixed_prompt", "text fixed"),
            "selfie": ("selfie", "photo_generation_selfie_fixed_prompt", "selfie fixed"),
            "portrait": ("selfie", "photo_generation_selfie_fixed_prompt", "selfie fixed"),
            "自拍": ("selfie", "photo_generation_selfie_fixed_prompt", "selfie fixed"),
            "人像": ("selfie", "photo_generation_selfie_fixed_prompt", "selfie fixed"),
            "edit": ("edit", "photo_generation_edit_fixed_prompt", "edit fixed"),
            "改图": ("edit", "photo_generation_edit_fixed_prompt", "edit fixed"),
            "修图": ("edit", "photo_generation_edit_fixed_prompt", "edit fixed"),
            "重绘": ("edit", "photo_generation_edit_fixed_prompt", "edit fixed"),
            "P图": ("edit", "photo_generation_edit_fixed_prompt", "edit fixed"),
        }
        for workflow_kind, (scope, config_key, marker) in expected.items():
            section, audit = self.harness._photo_generation_workflow_fixed_prompt_section(
                workflow_kind
            )
            self.assertEqual(audit["scope"], scope)
            self.assertEqual(audit["config_key"], config_key)
            self.assertIn(marker, section.positive)
            self.assertTrue(section.protected)
            self.assertTrue(section.sanitize_conflicts)

        script = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        for key in keys:
            self.assertIn(key, script)


if __name__ == "__main__":
    unittest.main()

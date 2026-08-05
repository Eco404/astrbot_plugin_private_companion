# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_private_companion.token_budget import (
    TokenBudgetMixin,
    _looks_like_upstream_llm_error_response,
)
from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


class _FallbackContext:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.kwargs.append(dict(kwargs))
        provider_id = str(kwargs.get("chat_provider_id") or "")
        self.calls.append(provider_id)
        result = self.responses.get(provider_id)
        if isinstance(result, Exception):
            raise result
        if hasattr(result, "completion_text"):
            return result
        return SimpleNamespace(role="assistant", completion_text=str(result or ""))


class _FallbackHarness(TokenBudgetMixin):
    def __init__(self, responses: dict[str, object], *, mode: str = "precision") -> None:
        self.context = _FallbackContext(responses)
        self.provider_config_mode = mode
        self.llm_provider_id = "primary"
        self.model_timeout_overrides = {}
        self.model_fallback_overrides = {}
        self.config = {}
        self.usage: list[dict] = []

    def _classify_llm_prompt(self, _prompt: str) -> str:
        return "other"

    def _is_llm_budget_exempt_task(self, _task: str) -> bool:
        return False

    def _daily_token_soft_limit_should_defer(self, _task: str) -> bool:
        return False

    def _llm_daily_budget_remaining(self) -> int:
        return 100000

    def _record_llm_usage(self, **kwargs) -> None:
        self.usage.append(kwargs)


class _UsageToolSet:
    @staticmethod
    def openai_schema() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "pc_generate_photo",
                    "description": "Capture a photo generation decision.",
                    "parameters": {
                        "type": "object",
                        "properties": {"prompt": {"type": "string"}},
                        "required": ["prompt"],
                    },
                },
            }
        ]


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_uses_the_budgeted_primary_provider_path(self) -> None:
        response = SimpleNamespace(
            role="assistant",
            completion_text="",
            tools_call_name=["pc_generate_photo"],
            tools_call_args=[{"kind": "selfie", "prompt": "portrait"}],
        )
        harness = _FallbackHarness({"primary": response})
        tools = _UsageToolSet()

        result = await harness._llm_tool_call(
            "take a photo",
            tools=tools,
            provider_id="primary",
            task="photo_reference_selection_trial",
            timeout_key="LLM_PROVIDER_ID",
        )

        self.assertIs(result, response)
        self.assertEqual(harness.context.calls, ["primary"])
        self.assertIs(harness.context.kwargs[0]["tools"], tools)
        self.assertTrue(harness.usage[0]["success"])
        self.assertIn("pc_generate_photo", harness.usage[0]["prompt"])
        self.assertIn("pc_generate_photo", harness.usage[0]["completion"])
        self.assertIn("portrait", harness.usage[0]["completion"])
        estimated = harness._extract_llm_usage(
            response,
            harness.usage[0]["prompt"],
            harness.usage[0]["completion"],
        )
        self.assertTrue(estimated["estimated"])
        self.assertGreater(
            estimated["prompt_tokens"],
            harness._estimate_token_count("take a photo"),
        )
        self.assertGreater(estimated["completion_tokens"], 0)

    async def test_tool_call_preserves_timeout_error_without_configured_timeout(self) -> None:
        harness = _FallbackHarness({"primary": asyncio.TimeoutError()})

        with self.assertRaisesRegex(TimeoutError, "模型任务 photo_reference_selection_trial 调用超时"):
            await harness._llm_tool_call(
                "take a photo",
                tools=_UsageToolSet(),
                provider_id="primary",
                task="photo_reference_selection_trial",
            )

        self.assertEqual(harness.context.calls, ["primary"])
        self.assertIn("调用超时", harness.usage[0]["error"])
        self.assertNotIn("NoneType", harness.usage[0]["error"])

    async def test_tool_call_stops_before_provider_when_daily_budget_is_exhausted(self) -> None:
        harness = _FallbackHarness({"primary": "unused"})
        skips: list[dict] = []
        harness._llm_daily_budget_remaining = lambda: 0
        harness._record_llm_budget_skip = lambda **kwargs: skips.append(kwargs)

        result = await harness._llm_tool_call(
            "take a photo",
            tools="trial-tools",
            provider_id="primary",
            task="photo_reference_selection_trial",
        )

        self.assertIsNone(result)
        self.assertEqual(harness.context.calls, [])
        self.assertEqual(skips[0]["provider_id"], "primary")

    async def test_primary_failure_uses_card_fallback_once(self) -> None:
        harness = _FallbackHarness({"primary": RuntimeError("primary down"), "backup": "ok"})
        harness.model_fallback_overrides = {"DAILY_PLAN_PROVIDER_ID": "backup"}
        result = await harness._llm_call(
            "plan",
            provider_id="primary",
            task="daily_plan",
            timeout_key="DAILY_PLAN_PROVIDER_ID",
        )
        self.assertEqual(result, "ok")
        self.assertEqual(harness.context.calls, ["primary", "backup"])
        self.assertFalse(harness.usage[0]["success"])
        self.assertTrue(harness.usage[1]["success"])

    async def test_empty_primary_response_uses_fallback(self) -> None:
        harness = _FallbackHarness({"primary": "", "backup": "fallback text"})
        harness.model_fallback_overrides = {"RESPONSE_REVIEW_PROVIDER_ID": "backup"}
        result = await harness._llm_call(
            "review",
            provider_id="primary",
            task="response_review",
            timeout_key="RESPONSE_REVIEW_PROVIDER_ID",
        )
        self.assertEqual(result, "fallback text")
        self.assertEqual(harness.context.calls, ["primary", "backup"])

    async def test_semantic_provider_error_uses_card_fallback(self) -> None:
        harness = _FallbackHarness(
            {
                "primary": "The prompt could not be submitted.",
                "backup": "人格化备用模型正文",
            }
        )
        harness.model_fallback_overrides = {"RESPONSE_REVIEW_PROVIDER_ID": "backup"}

        result = await harness._llm_call(
            "review",
            provider_id="primary",
            task="proactive_message_fallback",
        )

        self.assertEqual(result, "人格化备用模型正文")
        self.assertEqual(harness.context.calls, ["primary", "backup"])
        self.assertFalse(harness.usage[0]["success"])
        self.assertEqual(harness.usage[0]["error"], "semantic_provider_error")
        self.assertEqual(harness.usage[0]["completion"], "The prompt could not be submitted.")
        self.assertTrue(harness.usage[1]["success"])

    async def test_native_error_role_uses_card_fallback(self) -> None:
        harness = _FallbackHarness(
            {
                "primary": SimpleNamespace(
                    role="err",
                    completion_text="opaque upstream failure",
                ),
                "backup": "备用模型正常正文",
            }
        )
        harness.model_fallback_overrides = {"RESPONSE_REVIEW_PROVIDER_ID": "backup"}

        result = await harness._llm_call(
            "review",
            provider_id="primary",
            task="response_review",
            timeout_key="RESPONSE_REVIEW_PROVIDER_ID",
        )

        self.assertEqual(result, "备用模型正常正文")
        self.assertEqual(harness.context.calls, ["primary", "backup"])
        self.assertEqual(harness.usage[0]["error"], "provider_error_role")

    async def test_normal_technical_text_does_not_use_card_fallback(self) -> None:
        normal_messages = (
            "你刚才说的 tool schema 我看懂了，先歇一会儿吧。",
            "那个页面显示 status disabled，晚点我陪你再看。",
            "别再盯着 traceback 了，先喝口水。",
            "工具调用失败这种提示确实很烦，但先别折腾了。",
        )
        for normal_text in normal_messages:
            with self.subTest(normal_text=normal_text):
                self.assertFalse(_looks_like_upstream_llm_error_response(normal_text))
                harness = _FallbackHarness(
                    {"primary": normal_text, "backup": "不应调用"}
                )
                harness.model_fallback_overrides = {
                    "RESPONSE_REVIEW_PROVIDER_ID": "backup"
                }
                result = await harness._llm_call(
                    "review",
                    provider_id="primary",
                    task="response_review",
                    timeout_key="RESPONSE_REVIEW_PROVIDER_ID",
                )
                self.assertEqual(result, normal_text)
                self.assertEqual(harness.context.calls, ["primary"])

    async def test_same_primary_and_fallback_is_not_retried(self) -> None:
        harness = _FallbackHarness({"primary": RuntimeError("down")})
        harness.model_fallback_overrides = {"DAILY_PLAN_PROVIDER_ID": "primary"}
        result = await harness._llm_call(
            "plan",
            provider_id="primary",
            task="daily_plan",
            timeout_key="DAILY_PLAN_PROVIDER_ID",
        )
        self.assertIsNone(result)
        self.assertEqual(harness.context.calls, ["primary"])

    async def test_quick_mode_uses_quick_card_fallback(self) -> None:
        harness = _FallbackHarness({"primary": RuntimeError("down"), "quick-backup": "ok"}, mode="quick")
        harness.model_fallback_overrides = {"COMPLEX_REASONING_PROVIDER_ID": "quick-backup"}
        result = await harness._llm_call(
            "plan",
            provider_id="primary",
            task="daily_plan",
            timeout_key="DAILY_PLAN_PROVIDER_ID",
        )
        self.assertEqual(result, "ok")
        self.assertEqual(harness.context.calls, ["primary", "quick-backup"])

    def test_fallback_config_is_normalized_and_ui_is_wired(self) -> None:
        normalized = _FallbackHarness._normalize_model_fallback_overrides(
            '{"DAILY_PLAN_PROVIDER_ID":" backup ","unknown":"ignored"}'
        )
        self.assertEqual(normalized, {"DAILY_PLAN_PROVIDER_ID": "backup"})
        root = Path(__file__).resolve().parents[1]
        provider_tree = (root / "pages" / "陪伴面板" / "js" / "panels" / "provider-tree.js").read_text(encoding="utf-8")
        app_js = (root / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data-provider-fallback-select", provider_tree)
        self.assertIn("model_fallback_overrides: fallbackOverrides", app_js)

    def test_ui_can_clear_a_saved_fallback_before_save(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        provider_tree_path = Path(__file__).resolve().parents[1] / "pages" / "陪伴面板" / "js" / "panels" / "provider-tree.js"
        script = f"""
global.window = {{}};
const fs = require("fs");
eval(fs.readFileSync({json.dumps(str(provider_tree_path), ensure_ascii=False)}, "utf8"));
const values = window.PrivateCompanionProviderTree.currentProviderFallbackValues({{
  state: {{
    overview: {{ settings: {{ model_fallback_overrides: {{ DAILY_PLAN_PROVIDER_ID: "saved-backup" }} }} }},
    providerFallbackDraft: {{ DAILY_PLAN_PROVIDER_ID: "" }},
  }},
  document: {{ querySelectorAll: () => [] }},
}});
process.stdout.write(JSON.stringify(values));
"""
        result = subprocess.run(
            [node, "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), {})

    def test_precision_ui_fallback_ignores_hidden_quick_provider_values(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        provider_tree_path = Path(__file__).resolve().parents[1] / "pages" / "陪伴面板" / "js" / "panels" / "provider-tree.js"
        script = f"""
global.window = {{}};
const fs = require("fs");
eval(fs.readFileSync({json.dumps(str(provider_tree_path), ensure_ascii=False)}, "utf8"));
const values = {{
  FAST_RESPONSE_PROVIDER_ID: "old-fast",
  COMPLEX_REASONING_PROVIDER_ID: "old-complex",
  LLM_PROVIDER_ID: "new-main",
  MAI_STYLE_PROVIDER_ID: "new-style",
}};
const common = {{
  noFallbackProviderKeys: new Set(),
  optionalNoFallbackProviderKeys: new Set(),
  state: {{}},
}};
const precision = window.PrivateCompanionProviderTree.resolveProviderId(
  {{ ...common, currentProviderConfigMode: () => "precision" }},
  "HISTORY_SUMMARY_PROVIDER_ID",
  values,
);
const quick = window.PrivateCompanionProviderTree.resolveProviderId(
  {{ ...common, currentProviderConfigMode: () => "quick" }},
  "HISTORY_SUMMARY_PROVIDER_ID",
  values,
);
process.stdout.write(JSON.stringify({{ precision, quick }}));
"""
        result = subprocess.run(
            [node, "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            json.loads(result.stdout),
            {"precision": "new-main", "quick": "old-complex"},
        )

    def test_precision_save_rebuilds_hidden_quick_bundle_from_visible_values(self) -> None:
        api = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
        values = {
            "FAST_RESPONSE_PROVIDER_ID": "old-fast",
            "COMPLEX_REASONING_PROVIDER_ID": "old-complex",
            "CREATIVE_MODEL_PROVIDER_ID": "old-creative",
            "PLUGIN_VISION_PROVIDER_ID": "old-vision",
            "LLM_PROVIDER_ID": "new-main",
            "MAI_STYLE_PROVIDER_ID": "new-style",
            "CREATIVE_PROVIDER_ID": "new-creative",
            "NARRATION_PROVIDER_ID": "new-narration",
            "PRIVATE_READING_VISION_PROVIDER_ID": "new-vision",
        }

        bundle = api._quick_bundle_from_precision(values)

        self.assertEqual(bundle["FAST_RESPONSE_PROVIDER_ID"], "new-style")
        self.assertEqual(bundle["COMPLEX_REASONING_PROVIDER_ID"], "new-main")
        self.assertEqual(bundle["CREATIVE_MODEL_PROVIDER_ID"], "new-creative")
        self.assertEqual(bundle["PLUGIN_VISION_PROVIDER_ID"], "new-narration")

    def test_page_api_accepts_and_serializes_fallback_map(self) -> None:
        api = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
        api.plugin = SimpleNamespace(_normalize_model_fallback_overrides=_FallbackHarness._normalize_model_fallback_overrides)
        api._schema_key_index_cache = None
        self.assertIn("model_fallback_overrides", api._allowed_setting_keys())
        saved = api._normalize_setting_value(
            "model_fallback_overrides",
            {"DAILY_PLAN_PROVIDER_ID": "backup"},
        )
        self.assertEqual(json.loads(saved), {"DAILY_PLAN_PROVIDER_ID": "backup"})


if __name__ == "__main__":
    unittest.main()

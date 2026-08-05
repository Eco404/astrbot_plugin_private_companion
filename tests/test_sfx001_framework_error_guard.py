from __future__ import annotations

import ast
import copy
from pathlib import Path
import re
from typing import Any
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _single_line(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _guard_host_type():
    """Load only the pure guard helpers, without importing AstrBot."""
    source = (ROOT / "proactive_message.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    mixin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ProactiveMessageMixin")
    names = {
        "_looks_like_internal_provider_error_text",
        "_looks_like_python_traceback_text",
        "_framework_agent_meta_summary_leak",
        "_framework_error_leak_kind",
    }
    namespace: dict[str, Any] = {"Any": Any, "re": re, "_single_line": _single_line}
    for node in mixin.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            module = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, str(ROOT / "proactive_message.py"), "exec"), namespace)

    class GuardHost:
        def _is_proactive_delivery_receipt_text(self, _text: str) -> bool:
            return False

        def _is_proactive_instruction_leak_text(self, _text: str) -> bool:
            return False

    for name in names:
        setattr(GuardHost, name, namespace[name])
    return GuardHost


GuardHost = _guard_host_type()


class FrameworkErrorGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = GuardHost()

    def test_technical_guidance_with_traceback_schema_and_image_url_is_allowed(self) -> None:
        text = "请把完整 traceback、调用栈、相关 tool schema 和页面报错截图贴出来，再检查 image_url 字段。"

        self.assertFalse(self.host._looks_like_internal_provider_error_text(text))
        self.assertEqual("", self.host._framework_error_leak_kind(text))

    def test_complete_python_traceback_has_its_own_reason_code(self) -> None:
        text = """Traceback (most recent call last):
  File \"main.py\", line 8, in <module>
    run()
ValueError: bad input"""

        self.assertTrue(self.host._looks_like_python_traceback_text(text))
        self.assertEqual("python_traceback", self.host._framework_error_leak_kind(text))

    def test_provider_request_error_is_still_blocked(self) -> None:
        text = "Provider API Error: Error code: 400 - Invalid request: tool schema is invalid"

        self.assertTrue(self.host._looks_like_internal_provider_error_text(text))
        self.assertEqual("provider_error", self.host._framework_error_leak_kind(text))

    def test_tool_loop_summary_is_still_blocked(self) -> None:
        text = "Agent reached max steps; forcing a final response after trying to send messages."

        self.assertEqual("tool_loop", self.host._framework_error_leak_kind(text))

    def test_send_guard_uses_reason_code_without_recording_the_body(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        hook = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "suppress_framework_error_leak_before_send"
        )
        hook_source = ast.get_source_segment(source, hook) or ""

        self.assertIn('getattr(self, "_framework_error_leak_kind", None)', hook_source)
        self.assertIn('marker_kind = "tool_loop"', hook_source)
        self.assertIn('reply_preview=""', hook_source)
        self.assertNotIn("tool_loop_summary", hook_source)


if __name__ == "__main__":
    unittest.main()

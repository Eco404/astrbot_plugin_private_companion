from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class EmotionE1PromptCacheTests(unittest.TestCase):
    def test_expression_decision_is_a_forced_dynamic_fragment(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        hook = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "inject_unified_relationship_expression"
        )
        calls = [
            node
            for node in ast.walk(hook)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_append_turn_prompt_fragment_by_position"
        ]
        self.assertEqual(1, len(calls))
        keywords = {item.arg: item.value for item in calls[0].keywords if item.arg}
        self.assertIsInstance(keywords.get("force_dynamic"), ast.Constant)
        self.assertTrue(keywords["force_dynamic"].value)

        assignments = [
            node
            for node in ast.walk(hook)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and "system_prompt" in ast.unparse(node)
            and "Companion expression" in ast.unparse(node)
        ]
        self.assertEqual([], assignments)

    def test_forced_dynamic_fragments_ignore_system_prompt_preference(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        helper = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_append_turn_prompt_fragment_by_position"
        )
        rendered = ast.unparse(helper)
        self.assertIn("force_dynamic", rendered)
        self.assertIn("and (not force_dynamic)", rendered)
        self.assertIn("prefer_extra_user_content=True", rendered)


if __name__ == "__main__":
    unittest.main()

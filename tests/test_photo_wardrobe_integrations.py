from __future__ import annotations

import ast
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _module_tree(name: str) -> ast.Module:
    return ast.parse((PLUGIN_ROOT / name).read_text(encoding="utf-8"), filename=name)


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


class PhotoWardrobeIntegrationTests(unittest.TestCase):
    def test_generation_chain_reuses_one_intent_for_selection_and_resolution(self) -> None:
        generate = _function(_module_tree("proactive_message.py"), "_generate_photo_image")
        calls = [node for node in ast.walk(generate) if isinstance(node, ast.Call)]

        analyze_calls = [call for call in calls if _call_name(call) == "analyze_photo_wardrobe"]
        select_call = next(
            call for call in calls if _call_name(call) == "_select_photo_reference_candidate_async"
        )
        resolve_call = next(
            call for call in calls if _call_name(call) == "resolve_photo_wardrobe_decision"
        )

        self.assertEqual(len(analyze_calls), 1)
        select_intent = next(
            keyword.value for keyword in select_call.keywords if keyword.arg == "wardrobe_intent"
        )
        resolve_intent = next(keyword.value for keyword in resolve_call.keywords if keyword.arg == "intent")
        self.assertIsInstance(select_intent, ast.Name)
        self.assertIsInstance(resolve_intent, ast.Name)
        self.assertEqual(select_intent.id, "wardrobe_intent")
        self.assertEqual(resolve_intent.id, "wardrobe_intent")

    def test_only_wardrobe_module_constructs_production_decisions(self) -> None:
        offenders: list[str] = []
        for path in PLUGIN_ROOT.glob("*.py"):
            if path.name == "photo_wardrobe_decision.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
            if any(
                isinstance(node, ast.Call) and _call_name(node) == "PhotoWardrobeDecision"
                for node in ast.walk(tree)
            ):
                offenders.append(path.name)

        self.assertEqual(offenders, [])

    def test_debug_schema_and_command_prompt_use_the_unified_decision_contract(self) -> None:
        proactive = (PLUGIN_ROOT / "proactive_message.py").read_text(encoding="utf-8")
        commands = (PLUGIN_ROOT / "command_handlers.py").read_text(encoding="utf-8")

        self.assertIn('"schema_version": 2', proactive)
        self.assertIn('"wardrobe_rule_id"', proactive)
        self.assertIn('"wardrobe_adjustments"', proactive)
        self.assertNotIn("_natural_photo_prompt_has_explicit_wardrobe_request", commands)
        self.assertIn("preserve character identity and stable appearance", commands)


if __name__ == "__main__":
    unittest.main()

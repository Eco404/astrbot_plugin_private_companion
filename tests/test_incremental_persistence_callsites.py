# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_private_companion.event_dispatch import EventDispatchMixin


ROOT = Path(__file__).resolve().parents[1]


def _private_handler() -> ast.AsyncFunctionDef:
    tree = ast.parse(
        (ROOT / "message_pipeline.py").read_text(encoding="utf-8"),
        filename="message_pipeline.py",
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_private_message"
    )


def _group_handler() -> ast.AsyncFunctionDef:
    tree = ast.parse(
        (ROOT / "message_pipeline.py").read_text(encoding="utf-8"),
        filename="message_pipeline.py",
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_group_message"
    )


def _if_node(function: ast.AST, condition: str) -> ast.If:
    return next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and ast.unparse(node.test) == condition
    )


def _if_nodes(function: ast.AST, condition: str) -> list[ast.If]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and ast.unparse(node.test) == condition
    ]


def _string_constants(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and type(item.value) is str
    }


class _SmartDebounceHarness(EventDispatchMixin):
    def __init__(self) -> None:
        self.data: dict = {}
        self.enable_message_debounce = True
        self.enable_smart_message_debounce = True
        self.smart_message_debounce_examples_limit = 8
        self.smart_message_debounce_learning_window_seconds = 8.0
        self._schedule_data_save = Mock()


class IncrementalPersistenceCallsiteTests(unittest.TestCase):
    def test_first_fast_smart_debounce_decision_is_recorded(self) -> None:
        harness = _SmartDebounceHarness()
        event = SimpleNamespace()

        wait = asyncio.run(
            harness._smart_message_debounce_wait_seconds_for_event(
                event,
                key=harness._semantic_buffer_key("private:user-1", "user-1"),
                text="在吗？",
                sender_id="user-1",
            )
        )

        self.assertEqual(0.0, wait)
        decisions = harness.data["smart_message_debounce"]["last_decisions"]
        self.assertEqual(1, len(decisions))
        self.assertEqual("complete", next(iter(decisions.values()))["decision"])

    def test_smart_debounce_followup_marks_its_durable_section(self) -> None:
        harness = _SmartDebounceHarness()
        key = harness._semantic_buffer_key("private:user-1", "user-1")
        harness.data["smart_message_debounce"] = {
            "last_decisions": {
                key: {
                    "ts": time.time(),
                    "text": "first",
                    "decision": "complete",
                }
            },
            "examples": [],
            "recent_logs": [],
        }

        changed = harness._maybe_record_smart_message_debounce_followup(
            scope="private:user-1",
            sender_id="user-1",
            text="continued",
            now=time.time(),
        )

        self.assertTrue(changed)
        self.assertEqual(
            "false_complete",
            harness.data["smart_message_debounce"]["examples"][0]["kind"],
        )
        harness._schedule_data_save.assert_not_called()

    def test_private_pipeline_marks_fast_path_meal_and_warmth_sections(self) -> None:
        handler = _private_handler()
        meal_branch = _if_node(handler, "fast_meal_care_result.get('foods')")
        warmth_branches = _if_nodes(handler, "fast_interaction_warmth_applied")

        self.assertIn("food_menu", _string_constants(meal_branch))
        self.assertTrue(
            any(
                {"state_conditions", "daily_state"}.issubset(_string_constants(branch))
                for branch in warmth_branches
            )
        )

    def test_private_pipeline_marks_normal_state_feedback_sections(self) -> None:
        handler = _private_handler()
        care_branches = _if_nodes(handler, "care_feedback_detected")
        food_branches = _if_nodes(handler, "food_feedback_detected")
        warmth_branches = _if_nodes(handler, "interaction_warmth_applied")

        self.assertTrue(
            any(
                "state_conditions" in _string_constants(branch)
                for branch in care_branches
            )
        )
        self.assertTrue(
            any(
                {
                    "last_food_state_feedback_at",
                    "last_food_state_feedback_text",
                }.issubset(_string_constants(branch))
                for branch in food_branches
            )
        )
        self.assertTrue(
            any(
                {"state_conditions", "daily_state"}.issubset(_string_constants(branch))
                for branch in warmth_branches
            )
        )

    def test_private_pipeline_uses_expression_feedback_source_sections(self) -> None:
        handler = _private_handler()
        feedback_branch = _if_node(handler, "expression_feedback")
        source = ast.unparse(feedback_branch)

        self.assertIn("updated_sections", source)
        self.assertIn("updated_rules", source)
        self.assertIn("expression_voice_profile", source)

    def test_private_pipeline_saves_smart_state_at_early_and_final_commit_points(
        self,
    ) -> None:
        handler = _private_handler()
        branches = _if_nodes(handler, "smart_debounce_state_changed")

        self.assertGreaterEqual(len(branches), 2)
        self.assertTrue(
            all(
                "smart_message_debounce" in _string_constants(branch)
                for branch in branches
            )
        )

    def test_group_registration_persists_worldbook_profile_sections(self) -> None:
        source = ast.unparse(_group_handler())

        self.assertIn("registration_payload", source)
        self.assertIn("worldbook_member_profiles", source)
        self.assertIn("worldbook_deleted_member_ids", source)
        self.assertIn("self._save_data_sync(sections=save_sections)", source)

    def test_proactive_message_has_no_implicit_full_save_calls(self) -> None:
        tree = ast.parse(
            (ROOT / "proactive_message.py").read_text(encoding="utf-8"),
            filename="proactive_message.py",
        )
        bare_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_save_data_sync"
            and not node.args
            and not node.keywords
        ]

        self.assertEqual([], bare_calls)

    def test_private_image_buffer_persists_smart_learning_state(self) -> None:
        source = (ROOT / "private_image.py").read_text(encoding="utf-8")
        self.assertIn(
            'scheduler(sections={"smart_message_debounce"})',
            source,
        )

    def test_proactive_only_meal_care_marks_food_menu(self) -> None:
        tree = ast.parse(
            (ROOT / "main.py").read_text(encoding="utf-8"),
            filename="main.py",
        )
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_record_proactive_only_private_feedback"
        )
        source = ast.unparse(handler)

        self.assertIn("meal_care_result = self._handle_meal_care_inbound", source)
        self.assertIn("save_sections.add('food_menu')", source)
        self.assertIn("self._schedule_data_save(sections=save_sections)", source)


if __name__ == "__main__":
    unittest.main()

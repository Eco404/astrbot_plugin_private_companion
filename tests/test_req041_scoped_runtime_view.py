from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from scoped_runtime_view import (
    overlay_group_runtime_view,
    overlay_private_runtime_view,
    scoped_approved_expression_rules,
)


ROOT = Path(__file__).resolve().parents[1]


def _method_from(path: Path, class_name: str, method_name: str, globals_map: dict):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    method = next(
        item for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0), method],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = dict(globals_map)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[method_name]


def _safe_int(value, default=0, minimum=0, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    result = max(minimum, result)
    return min(maximum, result) if maximum is not None else result


class ScopedRuntimeViewTests(unittest.TestCase):
    def test_private_overlay_accepts_only_current_domain_allowlist(self) -> None:
        base = {
            "nickname": "legacy", "relationship_score": 88,
            "private_sentinel": "legacy-only",
        }
        projection = {
            "ok": True,
            "fields": {
                "nickname": "private-a",
                "companion_memory": {"items": ["private-a-memory"]},
                "relationship_score": -999,
                "recent_messages": ["group-a-must-not-enter"],
            },
        }
        view = overlay_private_runtime_view(base, projection)
        self.assertEqual("private-a", view["nickname"])
        self.assertEqual(88, view["relationship_score"])
        self.assertNotIn("recent_messages", view)
        self.assertEqual("new", view["req041_scoped_read_generation"])

    def test_group_overlay_keeps_shared_and_current_member_only(self) -> None:
        base = {
            "recent_messages": ["legacy"],
            "members": {"a": {"name": "legacy-a"}, "b": {"name": "keep-b"}},
        }
        shared = {
            "ok": True,
            "fields": {
                "recent_messages": ["group-a"],
                "companion_memory": {"items": ["private-must-not-enter"]},
            },
        }
        member = {
            "ok": True,
            "fields": {
                "name": "a-in-group-a", "recent_phrases": ["a-group-a"],
                "nickname": "private-name-must-not-enter",
            },
        }
        view = overlay_group_runtime_view(base, shared, sender_id="a", member_projection=member)
        self.assertEqual(["group-a"], view["recent_messages"])
        self.assertEqual("a-in-group-a", view["members"]["a"]["name"])
        self.assertEqual("keep-b", view["members"]["b"]["name"])
        self.assertNotIn("companion_memory", view)
        self.assertNotIn("nickname", view["members"]["a"])

    def test_dirty_or_unreconciled_projection_does_not_open_new_read(self) -> None:
        base = {"nickname": "legacy"}
        self.assertIs(base, overlay_private_runtime_view(base, {"ok": False, "fields": {"nickname": "stale"}}))
        self.assertIs(base, overlay_group_runtime_view(base, {"ok": False}, sender_id="a"))

    def test_scoped_rule_empty_is_authoritative_and_never_legacy_fallback(self) -> None:
        self.assertIsNone(scoped_approved_expression_rules({"expression_profile": {}}))
        self.assertEqual([], scoped_approved_expression_rules({
            "req041_scoped_read_generation": "new",
        }))
        self.assertEqual([], scoped_approved_expression_rules({
            "req041_scoped_read_generation": "new_unavailable",
            "expression_profile": {"learned_rules": [{"id": "stale-must-not-enter"}]},
        }))
        rules = scoped_approved_expression_rules({
            "req041_scoped_read_generation": "new",
            "expression_profile": {
                "learned_rules": [{"id": "private-a"}],
                "pending_rules": [{"id": "pending-must-not-enter"}],
            },
        })
        self.assertEqual([{"id": "private-a"}], rules)


class _ExpressionHarness:
    enable_expression_learning = True

    def __init__(self) -> None:
        self.legacy_reads = 0

    @staticmethod
    def _expression_private_application_enabled(_target):
        return True

    @staticmethod
    def _expression_group_application_enabled(_target):
        return True

    def _expression_voice_profile(self):
        self.legacy_reads += 1
        return {"learned_rules": [{"id": "legacy-cross-domain", "evidence_count": 9}]}

    @staticmethod
    def _expression_companion_context(**_kwargs):
        return {}

    @staticmethod
    def _select_learned_expression_rules(rules, **_kwargs):
        return list(rules or [])[:2]

    @staticmethod
    def _format_expression_rule_bundle_line(rule):
        return f"- {rule['id']}"


_ExpressionHarness._expression_voice_selection = _method_from(
    ROOT / "user_memory.py",
    "UserMemoryMixin",
    "_expression_voice_selection",
    {
        "_safe_int": _safe_int,
        "scoped_approved_expression_rules": scoped_approved_expression_rules,
    },
)


class ScopedLearningSelectionTests(unittest.TestCase):
    def test_new_private_scope_selects_only_its_approved_rules(self) -> None:
        harness = _ExpressionHarness()
        result = harness._expression_voice_selection(
            scope="private", target_id="user-a", inbound_text="hello",
            context_owner={
                "req041_scoped_read_generation": "new",
                "expression_profile": {
                    "learned_rules": [{"id": "private-a", "evidence_count": 2}],
                    "pending_rules": [{"id": "pending-a", "evidence_count": 7}],
                },
            },
        )
        self.assertEqual(["private-a"], [item["id"] for item in result["rules"]])
        self.assertNotIn("legacy-cross-domain", result["prompt"])
        self.assertEqual("current_namespace", result["selection_scope"])
        self.assertEqual(0, harness.legacy_reads)

    def test_new_group_with_no_rule_does_not_fall_back_to_other_domains(self) -> None:
        harness = _ExpressionHarness()
        result = harness._expression_voice_selection(
            scope="group", target_id="group-b", inbound_text="hello",
            context_owner={
                "req041_scoped_read_generation": "new",
                "expression_profile": {"pending_rules": [{"id": "pending-b"}]},
            },
        )
        self.assertEqual([], result["rules"])
        self.assertEqual("", result["prompt"])
        self.assertEqual(0, harness.legacy_reads)

    def test_legacy_generation_retains_official_aggregate_until_cutover(self) -> None:
        harness = _ExpressionHarness()
        result = harness._expression_voice_selection(
            scope="private", target_id="legacy-user", context_owner={"user_id": "legacy-user"},
        )
        self.assertEqual(["legacy-cross-domain"], [item["id"] for item in result["rules"]])
        self.assertEqual("legacy_aggregate", result["selection_scope"])
        self.assertEqual(1, harness.legacy_reads)

    def test_scoped_reconciliation_window_fails_closed_without_legacy_aggregate(self) -> None:
        harness = _ExpressionHarness()
        result = harness._expression_voice_selection(
            scope="private", target_id="user-a",
            context_owner={
                "req041_scoped_read_generation": "new_unavailable",
                "expression_profile": {
                    "learned_rules": [{"id": "stale-private-a", "evidence_count": 9}],
                },
            },
        )
        self.assertEqual([], result["rules"])
        self.assertEqual("", result["prompt"])
        self.assertEqual(0, harness.legacy_reads)


class _Router:
    def __init__(self) -> None:
        self.finished = []

    @staticmethod
    def begin(user, **_kwargs):
        return {"user": {**user, "relationship_score": 91, "req041_read_generation": "new"}, "chain_id": "c1"}

    def finish(self, chain_id):
        self.finished.append(chain_id)


class _SnapshotHarness:
    def __init__(self) -> None:
        self.req041_relationship_read_router = _Router()

    @staticmethod
    def _req041_scoped_private_read_view(_event, user):
        return {**user, "nickname": "scoped-name", "req041_scoped_read_generation": "new"}


_SnapshotHarness._req041_relationship_snapshot_view = _method_from(
    ROOT / "main.py",
    "PrivateCompanionPlugin",
    "_req041_relationship_snapshot_view",
    {"_single_line": lambda value, limit=80: str(value or "")[:limit], "uuid": uuid},
)


class BackgroundSnapshotTests(unittest.TestCase):
    def test_background_snapshot_composes_relationship_then_private_scope(self) -> None:
        harness = _SnapshotHarness()
        view = harness._req041_relationship_snapshot_view(
            {"user_id": "a", "relationship_score": 1}, source="test",
        )
        self.assertEqual(91, view["relationship_score"])
        self.assertEqual("scoped-name", view["nickname"])
        self.assertEqual(["c1"], harness.req041_relationship_read_router.finished)


class ConsumerWiringTests(unittest.TestCase):
    def test_passive_and_proactive_consumers_are_wired_to_scoped_snapshots(self) -> None:
        passive = (ROOT / "passive_state_pipeline.py").read_text(encoding="utf-8")
        proactive = (ROOT / "proactive_message.py").read_text(encoding="utf-8")
        self.assertLess(
            passive.index("private_user = scoped_getter(event, private_user)"),
            passive.index("preferred_address = _single_line("),
        )
        self.assertIn("existing_scoped_group = getattr(event, \"req041_scoped_group_read_view\", None)", passive)
        self.assertIn("snapshot_getter(user, source=\"proactive_chat_bridge\")", proactive)

    def test_admin_nickname_and_style_write_person_facts(self) -> None:
        page = (ROOT / "page_api_users_groups.py").read_text(encoding="utf-8")
        self.assertIn("_req041_update_unified_profile_facts", page)
        self.assertIn('profile_fact_changes["preferred_address"] = user["nickname"]', page)
        self.assertIn('profile_fact_changes["style"] = user["style"]', page)
        self.assertIn("legacy_profile_before", page)
        self.assertIn("user.pop(key, None)", page)

    def test_portrait_bridge_carries_the_formal_scoped_namespace(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('"private_companion_namespace_context"', source)
        self.assertIn('request["namespace_context"] = dict(namespace_context)', source)
        self.assertIn('request["namespace_context"] = namespace_context.to_dict()', source)

    def test_sync_save_invalidates_scoped_projection_before_persisting(self) -> None:
        source = (ROOT / "core_store.py").read_text(encoding="utf-8")
        method = source[source.index("    def _save_data_sync(self):"):source.index("    def _save_data_now_sync", source.index("    def _save_data_sync(self):"))]
        self.assertLess(method.index("_req041_schedule_scoped_sync"), method.index("_active_persona_scope"))


if __name__ == "__main__":
    unittest.main()

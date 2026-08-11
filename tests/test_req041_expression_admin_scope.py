from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time
import unittest

from expression_scope_ownership import (
    ExpressionScopeError,
    bind_expression_item,
    bind_expression_profile,
)


ROOT = Path(__file__).resolve().parents[1]


def _method(name: str, globals_map: dict):
    tree = ast.parse((ROOT / "page_api.py").read_text(encoding="utf-8"))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPageApi")
    method = next(node for node in class_node.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0), method],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = dict(globals_map)
    exec(compile(module, str(ROOT / "page_api.py"), "exec"), namespace)
    return namespace[name]


@dataclass(frozen=True)
class Context:
    kind: str = "private"
    persona_id: str = "default"
    identity_id: str = "person-a"
    group_id: str = ""
    assurance: str = "verified"
    profile_status: str = "active"
    policy_version: str = "req041-v1"
    migration_epoch: str = "epoch-a"

    def errors(self):
        return []


class Harness:
    max_learned_expression_items = 60

    def __init__(self):
        self.plugin = self
        self.req041_scoped_projection_sync = object()
        self.resolved_context = Context()

    @staticmethod
    def _single_line(value, limit=100):
        return " ".join(str(value or "").split())[:limit]

    @staticmethod
    def _int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _expression_bind_profile_scope(self, profile, context, *, bump_revision):
        result = bind_expression_profile(profile, context, bump_revision=bump_revision)
        states = {
            "pending_rules": ("pending", ""),
            "learned_rules": ("approved", "legacy_migration"),
            "rejected_rules": ("rejected", "administrator"),
            "revoked_rules": ("revoked", "administrator"),
        }
        for key, (state, default_actor) in states.items():
            rows = []
            for item in result.get(key, []) if isinstance(result.get(key), list) else []:
                existing = item.get("scope_binding") if isinstance(item.get("scope_binding"), dict) else {}
                rows.append(bind_expression_item(
                    item, context, approval_state=state,
                    approved_by=str(existing.get("approved_by") or default_actor),
                ))
            result[key] = rows
        return result

    @staticmethod
    def _normalize_group_identity_id(value):
        return str(value or "").strip().lower()

    def _req041_scoped_context_for_user(self, _owner, **_kwargs):
        return self.resolved_context

    def _req041_scoped_group_context(self, _group_id, **_kwargs):
        return Context(kind="group_shared", identity_id="", group_id="group-a")


GLOBALS = {
    "Any": object,
    "deepcopy": deepcopy,
    "datetime": datetime,
    "time": time,
    "ExpressionScopeError": ExpressionScopeError,
    "bind_expression_item": bind_expression_item,
    "bind_expression_profile": bind_expression_profile,
}
for _name in (
    "_expression_admin_scope_context",
    "_expression_prepare_admin_profile",
    "_expression_validate_admin_revision",
    "_expression_item_content",
    "_expression_finalize_admin_profile",
    "_apply_expression_profile_action",
):
    setattr(Harness, _name, _method(_name, GLOBALS))


def _rule(rule_id: str, family: str, context: Context) -> dict:
    return bind_expression_item(
        {
            "id": rule_id, "family_id": family, "kind": "style",
            "situation": "问候", "pattern": "好呀____", "instruction": "自然改写",
            "evidence_count": 2,
        },
        context, approval_state="pending",
    )


class ExpressionAdminScopeTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.context = Context()

    def owner(self):
        profile = bind_expression_profile({
            "pending_rules": [
                _rule("style-a", "family-a", self.context),
                bind_expression_item(
                    {
                        "id": "grammar-a", "family_id": "family-a", "kind": "grammar",
                        "situation": "问候", "pattern": "省略主语短句", "instruction": "使用短句",
                        "evidence_count": 2,
                    },
                    self.context, approval_state="pending",
                ),
            ],
        }, self.context)
        return {"expression_profile": profile}

    @staticmethod
    def revisions(owner):
        profile = owner["expression_profile"]
        return {
            "expected_scope_revision": profile["scope_revision"],
            "expected_item_revisions": {
                item["id"]: item["scope_binding"]["revision"]
                for item in profile["pending_rules"]
            },
        }

    def test_approval_binds_administrator_and_advances_both_revisions(self):
        owner = self.owner()
        payload = {**self.revisions(owner), "expression_action": "approve_rule_group", "rule_family_id": "family-a"}
        prepared = self.harness._expression_prepare_admin_profile(owner, self.context)
        self.harness._expression_validate_admin_revision(prepared, payload)
        before = deepcopy(prepared)
        message = self.harness._apply_expression_profile_action(owner, payload)
        self.harness._expression_finalize_admin_profile(owner, before, self.context)
        profile = owner["expression_profile"]
        self.assertIn("已通过规则组", message)
        self.assertEqual(2, profile["scope_revision"])
        self.assertEqual([], profile["pending_rules"])
        self.assertEqual({"style-a", "grammar-a"}, {item["id"] for item in profile["learned_rules"]})
        self.assertTrue(all(item["scope_binding"]["approval_state"] == "approved" for item in profile["learned_rules"]))
        self.assertTrue(all(item["scope_binding"]["approved_by"] == "administrator" for item in profile["learned_rules"]))
        self.assertTrue(all(item["scope_binding"]["revision"] == 2 for item in profile["learned_rules"]))

    def test_stale_profile_revision_is_rejected_without_mutation(self):
        owner = self.owner()
        payload = {**self.revisions(owner), "expression_action": "approve_rule_group", "rule_family_id": "family-a"}
        before = deepcopy(owner)
        owner["expression_profile"] = bind_expression_profile(
            owner["expression_profile"], self.context, bump_revision=True,
        )
        with self.assertRaisesRegex(ValueError, "已被其他操作更新"):
            self.harness._expression_validate_admin_revision(owner["expression_profile"], payload)
        self.assertEqual(before["expression_profile"]["pending_rules"], owner["expression_profile"]["pending_rules"])

    def test_stale_or_incomplete_item_revision_is_rejected(self):
        owner = self.owner()
        payload = {**self.revisions(owner), "rule_family_id": "family-a"}
        payload["expected_item_revisions"] = {"style-a": 1}
        with self.assertRaisesRegex(ValueError, "表达项已被其他操作更新"):
            self.harness._expression_validate_admin_revision(owner["expression_profile"], payload)

    def test_reject_and_delete_keep_bounded_audit_state(self):
        owner = self.owner()
        payload = {**self.revisions(owner), "expression_action": "reject_rule_group", "rule_family_id": "family-a"}
        before = deepcopy(owner["expression_profile"])
        self.harness._apply_expression_profile_action(owner, payload)
        self.harness._expression_finalize_admin_profile(owner, before, self.context)
        rejected = owner["expression_profile"]["rejected_rules"]
        self.assertEqual(2, len(rejected))
        self.assertTrue(all(item["scope_binding"]["approval_state"] == "rejected" for item in rejected))
        self.assertTrue(all(item["scope_binding"]["revision"] == 2 for item in rejected))

        approved = bind_expression_item(
            {**_rule("enabled", "enabled-family", self.context), "review_status": "approved"},
            self.context, approval_state="approved", approved_by="administrator", bump_revision=True,
        )
        profile = owner["expression_profile"]
        profile["learned_rules"] = [approved]
        before = deepcopy(profile)
        self.harness._apply_expression_profile_action(owner, {
            "expression_action": "delete_rule_group", "rule_family_id": "enabled-family",
        })
        self.harness._expression_finalize_admin_profile(owner, before, self.context)
        revoked = owner["expression_profile"]["revoked_rules"]
        self.assertEqual("revoked", revoked[0]["scope_binding"]["approval_state"])
        self.assertEqual(3, revoked[0]["scope_binding"]["revision"])

    def test_cross_namespace_preparation_fails_closed(self):
        owner = self.owner()
        with self.assertRaisesRegex(ValueError, "作用域校验失败"):
            self.harness._expression_prepare_admin_profile(
                owner,
                Context(identity_id="person-b"),
            )

    def test_raw_group_locator_cannot_override_container_identity(self):
        with self.assertRaisesRegex(ValueError, "群来源标识"):
            self.harness._expression_admin_scope_context(
                "group", "group-a", {"group_id": "group-b"},
            )

    def test_unresolved_private_admin_target_fails_closed(self):
        self.harness.resolved_context = None
        with self.assertRaisesRegex(ValueError, "没有可写的正式身份作用域"):
            self.harness._expression_admin_scope_context(
                "private", "raw-user", {"user_id": "raw-user"},
            )


if __name__ == "__main__":
    unittest.main()

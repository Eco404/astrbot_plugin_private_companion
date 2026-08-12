from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import hmac
import json
from pathlib import Path
import types
from typing import Any
import unittest

from unified_person_registry import UnifiedPersonRegistry


ROOT = Path(__file__).resolve().parents[1]


def _identity(subject: str) -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1",
        "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user",
        "platform_subject_id": subject,
    }


class _Request:
    payload: dict[str, Any] = {}

    async def get_json(self, silent: bool = True) -> dict[str, Any]:
        del silent
        return copy.deepcopy(self.payload)


def _load_page_unlink():
    path = ROOT / "page_api_users_groups.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "PrivateCompanionPageApiUsersGroupsMixin"
    )
    names = {
        "_identity_unlink_confirmation",
        "_safe_identity_unlink_result",
        "unlink_unified_identity",
    }
    methods = [
        copy.deepcopy(node) for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=methods, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": Any,
        "hashlib": hashlib,
        "hmac": hmac,
        "json": json,
        "request": _Request(),
        "logger": types.SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        "_safe_int": lambda value, default=0: int(value or default),
    }
    exec(compile(module, str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}, namespace["request"]


PAGE_METHODS, PAGE_REQUEST = _load_page_unlink()


class _AsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _PageHost:
    _identity_unlink_confirmation = staticmethod(PAGE_METHODS["_identity_unlink_confirmation"])
    _safe_identity_unlink_result = staticmethod(PAGE_METHODS["_safe_identity_unlink_result"])
    unlink_unified_identity = PAGE_METHODS["unlink_unified_identity"]

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"users": {}}
        self.registry = UnifiedPersonRegistry(self.data)
        created = self.registry.create_or_link(
            _identity("10001"), profile={"display_name": "A"}, operation_id="create-a"
        )
        self.person_id = created["person_id"]
        linked = self.registry.link_identity(
            self.person_id, _identity("10002"), operation_id="link-b"
        )
        assert linked["ok"]
        self.data["users"] = {
            "10001": {
                "user_id": "10001", "identity_subject_id": "10001",
                "unified_person_id": self.person_id,
            },
            "10002": {
                "user_id": "10002", "identity_subject_id": "10002",
                "unified_person_id": self.person_id,
            },
        }
        self.plugin = self
        self._data_lock = _AsyncLock()
        self.saved = 0

    def _page_unified_person_registry(self):
        return self.registry

    @staticmethod
    def _single_line(value: Any, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    @staticmethod
    def _ok(value: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": value}

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"ok": False, "error": message}

    def _schedule_data_save(self) -> None:
        self.saved += 1


class IdentityAdminUiTests(unittest.TestCase):
    def test_safe_summary_and_exact_resolver_never_expose_subject(self) -> None:
        host = _PageHost()
        resolved = host.registry.identity_for_person_subject(host.person_id, "10002")
        self.assertEqual("10002", resolved["platform_subject_id"])
        summary = host.registry.safe_admin_person_summary(host.person_id, "10002")
        self.assertTrue(summary["linked"])
        self.assertTrue(summary["current_identity_linked"])
        self.assertEqual(2, summary["active_identity_count"])
        encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("10001", encoded)
        self.assertNotIn("10002", encoded)
        self.assertNotIn("identity_key", encoded)

    def test_page_unlink_derives_exact_identity_from_selected_user(self) -> None:
        host = _PageHost()
        PAGE_REQUEST.payload = {
            "person_id": host.person_id,
            "user_id": "10002",
            "operation_id": "page-unlink-b",
            "dry_run": True,
        }
        preview = asyncio.run(host.unlink_unified_identity())
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["data"]["result"]["ok"])
        self.assertEqual("migration_dry_run", preview["data"]["result"]["code"])
        self.assertNotIn("identity_key", json.dumps(preview, ensure_ascii=False))
        self.assertNotIn("checkpoint", json.dumps(preview, ensure_ascii=False))

        PAGE_REQUEST.payload["dry_run"] = False
        PAGE_REQUEST.payload["confirmation_token"] = preview["data"]["result"]["confirmation_token"]
        applied = asyncio.run(host.unlink_unified_identity())
        self.assertTrue(applied["ok"])
        self.assertEqual("identity_unlinked", applied["data"]["result"]["code"])
        self.assertIsNone(
            host.registry.identity_for_person_subject(host.person_id, "10002")
        )
        self.assertEqual(1, host.saved)

    def test_page_unlink_rejects_stale_or_missing_preview_confirmation(self) -> None:
        host = _PageHost()
        PAGE_REQUEST.payload = {
            "person_id": host.person_id,
            "user_id": "10002",
            "operation_id": "page-unlink-stale",
            "dry_run": False,
        }
        self.assertFalse(asyncio.run(host.unlink_unified_identity())["ok"])
        PAGE_REQUEST.payload["confirmation_token"] = "0" * 64
        rejected = asyncio.run(host.unlink_unified_identity())
        self.assertFalse(rejected["ok"])
        self.assertIsNotNone(
            host.registry.identity_for_person_subject(host.person_id, "10002")
        )

    def test_official_v620_user_workspace_contains_safe_identity_lifecycle(self) -> None:
        english = (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8")
        chinese = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(english, chinese)
        self.assertIn('["identity","身份与隔离"]', english)
        self.assertIn('data-identity-action="archive"', english)
        self.assertIn('postJson(endpoint, body)', english)
        self.assertIn('confirmation_token: preview.confirmationToken', english)
        self.assertIn("不能绕过统一数据链直接删除", (ROOT / "page_api_users_groups.py").read_text(encoding="utf-8"))
        self.assertNotIn("identity.identity_key", english)


if __name__ == "__main__":
    unittest.main()

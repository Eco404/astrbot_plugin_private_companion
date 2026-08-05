from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import sys
import time
import types
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "req036_companion"
if PACKAGE not in sys.modules:
    module = types.ModuleType(PACKAGE)
    module.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = module

from req036_companion.unified_person_registry import UnifiedPersonRegistry
from req036_companion.unified_profile_contract import build_profile_dto, build_person_ref, validate_profile_dto
from req036_companion.unified_profile_service import (
    DEFAULT_UNAUTHORIZED_PRIVATE_REPLY,
    capability_summary,
    default_capabilities,
    migrate_legacy_capabilities,
    private_companion_gate,
    proactive_private_gate,
    rollback_legacy_capabilities,
    update_capabilities,
)


def _identity(subject_id: str) -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1",
        "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user",
        "platform_subject_id": subject_id,
    }


def _load_method(name: str) -> Any:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPlugin")
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    method = copy.deepcopy(method)
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "AstrMessageEvent": Any,
        "ProviderRequest": Any,
        "DEFAULT_UNAUTHORIZED_PRIVATE_REPLY": DEFAULT_UNAUTHORIZED_PRIVATE_REPLY,
        "_now_ts": lambda: 100.0,
        "_single_line": lambda value, limit=240: str(value or "").strip()[:limit],
    }
    exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
    return namespace[name]


REQ036_LLM_GATE = _load_method("guard_req036_private_capability_before_llm")
REQ036_GROUP_GATE = _load_method("guard_req036_group_portrait_queries")
REQ036_COMMAND_GATE = _load_method("companion_command")


def _load_sync_plugin_method(name: str) -> Any:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPlugin")
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == name)
    method = copy.deepcopy(method)
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "_single_line": lambda value, limit=120: str(value or "").strip()[:limit],
        "_safe_int": lambda value, default=0, minimum=0: max(minimum, int(value or default)),
        "req036_update_capabilities": update_capabilities,
    }
    exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
    return namespace[name]


REQ036_CONFIGURED_TARGET_MIGRATION = _load_sync_plugin_method("_req036_migrate_configured_target_capability")


def _load_proactive_target_sync() -> Any:
    source = (ROOT / "proactive.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ProactiveMixin")
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_sync_configured_targets")
    method = copy.deepcopy(method)
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "req036_capability_summary": capability_summary,
        "req036_update_capabilities": update_capabilities,
        "_safe_int": lambda value, default=0, minimum=0: max(minimum, int(value or default)),
        "_safe_float": lambda value, default=0.0: float(value or default),
        "_now_ts": lambda: 100.0,
    }
    exec(compile(module, str(ROOT / "proactive.py"), "exec"), namespace)
    return namespace["_sync_configured_targets"]


REQ036_CONFIGURED_TARGET_SYNC = _load_proactive_target_sync()


def _load_user_list_method() -> Any:
    source = (ROOT / "page_api_users_groups.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPageApiUsersGroupsMixin"
    )
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_users")
    module = ast.Module(body=[copy.deepcopy(method)], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "logger": types.SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        ),
        "time": time,
    }
    exec(compile(module, str(ROOT / "page_api_users_groups.py"), "exec"), namespace)
    return namespace["list_users"]


REQ036_USER_LIST = _load_user_list_method()


class _PrivateEvent:
    def __init__(self) -> None:
        self.stopped = False

    @staticmethod
    def is_private_chat() -> bool:
        return True

    @staticmethod
    def get_sender_id() -> str:
        return "u-1"

    def stop_event(self) -> None:
        self.stopped = True


class _GateHost:
    def __init__(self) -> None:
        self.data = {"users": {"u-1": {"unified_profile_capabilities": default_capabilities()}}}
        self.replies: list[str] = []
        self.memory_calls = 0
        self.tool_calls = 0
        self.relationship_calls = 0

    @staticmethod
    def _canonical_private_user_id(value: str) -> str:
        return value

    @staticmethod
    def _req036_private_gate_for_user(user: Any) -> dict[str, Any]:
        return private_companion_gate(user, "固定拒绝文本")

    async def _req036_reject_unauthorized_private_event(self, event: Any, gate: dict[str, Any]) -> None:
        self.replies.append(str(gate["reply"]))
        event.private_companion_req036_denied = True
        event.stop_event()


class _CommandEvent(_PrivateEvent):
    message_str = "陪伴 状态"


class _CommandGateHost:
    def __init__(self) -> None:
        self._data_lock = asyncio.Lock()
        self.user = {"unified_profile_capabilities": default_capabilities()}
        self.replies: list[str] = []
        self.qzone_calls = 0
        self.attached_sources: list[str] = []

    @staticmethod
    def _sender_display_name(_event: Any) -> str:
        return "测试用户"

    def _ensure_auto_private_user_profile(self, _event: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
        return self.user, False

    def _req036_attach_unified_profile_context(self, _event: Any, **kwargs: Any) -> dict[str, Any]:
        self.attached_sources.append(str(kwargs.get("source") or ""))
        return {"state": "profile_exact"}

    @staticmethod
    def _schedule_data_save() -> None:
        return None

    @staticmethod
    def _req036_private_gate_for_user(user: Any) -> dict[str, Any]:
        return private_companion_gate(user, "固定拒绝文本")

    async def _req036_reject_unauthorized_private_event(self, event: Any, gate: dict[str, Any]) -> None:
        self.replies.append(str(gate["reply"]))
        event.private_companion_req036_denied = True
        event.stop_event()

    def _qzone_note_event_bot(self, _event: Any) -> None:
        self.qzone_calls += 1


class _GroupEvent:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.stopped = False

    @staticmethod
    def get_sender_id() -> str:
        return "u-1"

    def stop_event(self) -> None:
        self.stopped = True


class _GroupGateHost:
    replies: list[str]

    def __init__(self) -> None:
        self.replies = []

    @staticmethod
    def _extract_group_id_from_event(event: Any) -> str:
        return "group-disabled"

    @staticmethod
    def _group_observation_event_text(event: Any) -> str:
        return str(event.message_str)

    @staticmethod
    def _req036_group_portrait_query_kind(text: Any) -> str:
        return "third_party"

    @staticmethod
    def _group_enabled_for_event(group_id: str) -> bool:
        raise AssertionError("REQ-036 privacy guard must not depend on group observation enablement")

    async def _reply(self, event: Any, text: str) -> None:
        self.replies.append(text)


class _ConfiguredTargetHost:
    def __init__(self) -> None:
        self.default_nickname = "你"
        self.user: dict[str, Any] = {"proactive_daily_limit": 0}
        self.delivery_bound = 0

    @staticmethod
    def _configured_target_ids() -> list[str]:
        return ["legacy-target"]

    def _get_user(self, user_id: str) -> dict[str, Any]:
        return self.user

    @staticmethod
    def _clear_pending_proactive_plan(user: dict[str, Any]) -> None:
        user["cleared"] = True

    def _ensure_private_user_umo(self, user_id: str, user: dict[str, Any]) -> None:
        self.delivery_bound += 1

    @staticmethod
    def _user_enabled_for_proactive(user_id: str, user: dict[str, Any]) -> bool:
        return False


class _LegacyTargetMigrationHost:
    @staticmethod
    def _canonical_private_user_id(value: str) -> str:
        return str(value or "").strip()

    @staticmethod
    def _configured_target_ids() -> list[str]:
        return ["legacy-target"]

    @staticmethod
    def _schedule_next_proactive(user: dict[str, Any], *, now: float) -> None:
        raise AssertionError("disabled proactive capability must not schedule")


class _UserListHost:
    def __init__(self) -> None:
        self.plugin = types.SimpleNamespace(
            _data_lock=asyncio.Lock(),
            data={"users": {"source-a": {}, "source-b": {}}},
        )

    @staticmethod
    def _query_int(_name: str, default: int, _minimum: int, _maximum: int) -> int:
        return default

    @staticmethod
    def _single_line(value: Any, limit: int = 80) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _user_summary(user_id: str, _user: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "unified_person_id": "person_" + "d" * 24,
            "last_seen_ts": 1,
        }

    @staticmethod
    def _ok(payload: dict[str, Any]) -> dict[str, Any]:
        return payload


class Req036CompanionTests(unittest.TestCase):
    def test_default_capabilities_and_hard_proactive_dependency(self) -> None:
        state = default_capabilities()
        self.assertFalse(state["private_companion_enabled"])
        self.assertFalse(state["proactive_private_enabled"])
        self.assertEqual("disabled", state["portrait_mode"])
        state["proactive_private_enabled"] = True
        self.assertEqual("proactive_requires_private_companion", proactive_private_gate({"unified_profile_capabilities": state})["code"])

    def test_administrator_update_records_only_capability_audit(self) -> None:
        user: dict[str, Any] = {"unified_profile_capabilities": default_capabilities()}
        result = update_capabilities(
            user,
            {"private_companion_enabled": True, "proactive_private_enabled": True, "portrait_mode": "use_existing"},
            actor_authorized=True,
            actor_id="page_administrator",
            target_identity="u-1",
            reason_code="test",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["capabilities"]["effective_proactive_private_enabled"])
        self.assertEqual("use_existing", result["capabilities"]["portrait_mode"])
        self.assertEqual("page_administrator", user["unified_profile_capability_audit"][-1]["actor_id"])
        self.assertNotIn("content", repr(user["unified_profile_capability_audit"]))

    def test_capability_migration_is_dry_idempotent_and_reversible(self) -> None:
        data: dict[str, Any] = {
            "users": {
                "legacy-on": {"enabled": True, "proactive_daily_limit": 2},
                "legacy-off": {"enabled": False, "proactive_daily_limit": 8},
            }
        }
        before = copy.deepcopy(data)
        preview = migrate_legacy_capabilities(data, operation_id="req036-test", dry_run=True)
        self.assertEqual("migration_dry_run", preview["code"])
        self.assertEqual(before, data)
        applied = migrate_legacy_capabilities(data, operation_id="req036-test", dry_run=False)
        self.assertEqual("migration_applied", applied["code"])
        self.assertTrue(data["users"]["legacy-on"]["unified_profile_capabilities"]["private_companion_enabled"])
        self.assertFalse(data["users"]["legacy-off"]["unified_profile_capabilities"]["proactive_private_enabled"])
        self.assertEqual("migration_idempotent_replay", migrate_legacy_capabilities(data, operation_id="req036-test", dry_run=False)["code"])
        self.assertEqual("migration_rolled_back", rollback_legacy_capabilities(data, operation_id="req036-test")["code"])
        self.assertEqual(before, {"users": data["users"]})

    def test_exact_identity_link_unlink_and_merge_require_review(self) -> None:
        store: dict[str, Any] = {}
        registry = UnifiedPersonRegistry(store)
        first = registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.assertTrue(first["ok"])
        same = registry.create_or_link(_identity("10001"), operation_id="create-1-repeat")
        self.assertEqual(first["person_id"], same["person_id"])
        self.assertFalse(same["changed"])
        linked = registry.link_identity(first["person_id"], _identity("telegram-10001"), operation_id="link-1")
        self.assertTrue(linked["changed"])
        self.assertTrue(
            registry.record_identity_source_event(
                first["person_id"],
                linked["identity_key"],
                "group:onebot:group-a",
                hashlib.sha256(b"source-event").hexdigest(),
                operation_id="event-1",
            )["ok"]
        )
        dry_run = registry.unlink_identity(first["person_id"], _identity("telegram-10001"), operation_id="unlink-1", dry_run=True)
        self.assertEqual("migration_dry_run", dry_run["code"])
        self.assertEqual(1, dry_run["replayable_event_count"])
        applied = registry.unlink_identity(first["person_id"], _identity("telegram-10001"), operation_id="unlink-1", dry_run=False)
        self.assertEqual("identity_unlinked", applied["code"])
        self.assertTrue(applied["changed"])
        self.assertTrue(registry.create_or_link(_identity("10002"), operation_id="create-2")["ok"])
        second = registry.resolve(_identity("10002"))
        merge = registry.preview_person_merge(first["person_id"], second["person_id"], operation_id="merge-preview")
        self.assertEqual("merge_manual_review_required", merge["code"])
        self.assertEqual(0, merge["write_count"])

    def test_unauthorized_private_llm_gate_clears_request_before_other_paths(self) -> None:
        host = _GateHost()
        event = _PrivateEvent()
        req = types.SimpleNamespace(
            system_prompt="secret",
            prompt="message",
            contexts=["memory"],
            extra_user_content_parts=["extra"],
            func_tool=object(),
            tools=["tool"],
            images=["image"],
            image_urls=["url"],
        )
        asyncio.run(REQ036_LLM_GATE(host, event, req))
        self.assertTrue(event.stopped)
        self.assertEqual(["固定拒绝文本"], host.replies)
        self.assertEqual("", req.system_prompt)
        self.assertEqual([], req.contexts)
        self.assertIsNone(req.func_tool)
        self.assertEqual(0, host.memory_calls)
        self.assertEqual(0, host.tool_calls)
        self.assertEqual(0, host.relationship_calls)

    def test_unauthorized_private_command_stops_before_qzone_or_command_actions(self) -> None:
        host = _CommandGateHost()
        event = _CommandEvent()
        asyncio.run(REQ036_COMMAND_GATE(host, event))
        self.assertTrue(event.stopped)
        self.assertEqual(["固定拒绝文本"], host.replies)
        self.assertEqual(0, host.qzone_calls)
        self.assertEqual(["private_command"], host.attached_sources)

    def test_contract_rejects_raw_conversation_fields(self) -> None:
        person = {
            "person_id": "person_" + "a" * 24,
            "resolved_identity_key": "chat-origin-v1:" + "b" * 64,
            "projection_revision": 1,
            "identity_assurance": "observed",
            "profile_status": "active",
        }
        dto = build_profile_dto(person_ref=build_person_ref(person), identity_summary={"display_name": "雪"})
        self.assertEqual([], validate_profile_dto(dto))
        dto["identity_summary"]["chat_text"] = "should not cross the bridge"
        self.assertIn("identity_summary_contains_forbidden_data", validate_profile_dto(dto))

    def test_private_entry_places_side_effects_after_capability_gate(self) -> None:
        source = (ROOT / "message_pipeline.py").read_text(encoding="utf-8")
        start = source.index("async def handle_private_message(")
        end = source.index("async def handle_group_message(", start)
        handler = source[start:end]
        self.assertLess(handler.index("if not private_gate.get(\"allowed\")"), handler.index("self._qzone_note_event_bot(event)"))
        self.assertLess(handler.index("if not private_gate.get(\"allowed\")"), handler.index("self._message_debounce_command_text(event, text)"))

    def test_group_portrait_query_classifier_distinguishes_self_from_third_party(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPlugin")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_req036_group_portrait_query_kind")
        method = copy.deepcopy(method)
        method.decorator_list = []
        module = ast.Module(body=[method], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"Any": Any, "re": __import__("re"), "_single_line": lambda value, _limit=240: str(value or "")[:_limit]}
        exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
        classify = namespace["_req036_group_portrait_query_kind"]
        self.assertEqual("self", classify("@bot 我喜欢什么"))
        self.assertEqual("self", classify("@bot 我爱吃什么"))
        self.assertEqual("third_party", classify("@bot 小王喜欢什么"))
        self.assertEqual("third_party", classify("@bot 小王有什么爱好"))

    def test_group_third_party_guard_applies_when_observation_is_disabled(self) -> None:
        host = _GroupGateHost()
        event = _GroupEvent("@bot 小王有什么爱好")
        asyncio.run(REQ036_GROUP_GATE(host, event))
        self.assertTrue(event.stopped)
        self.assertEqual(["这个我不方便替别人整理啦。"], host.replies)

    def test_configured_target_migrates_once_then_respects_capability_freeze(self) -> None:
        host = _ConfiguredTargetHost()
        REQ036_CONFIGURED_TARGET_SYNC(host)
        self.assertTrue(host.user["unified_profile_capabilities"]["private_companion_enabled"])
        self.assertFalse(host.user["unified_profile_capabilities"]["proactive_private_enabled"])
        self.assertEqual("legacy_configured_target_migration", host.user["unified_profile_capabilities"]["grant_source"])
        self.assertEqual(1, host.delivery_bound)
        update_capabilities(
            host.user,
            {"private_companion_enabled": False},
            actor_authorized=True,
            actor_id="page_administrator",
            target_identity="legacy-target",
        )
        REQ036_CONFIGURED_TARGET_SYNC(host)
        self.assertFalse(host.user["unified_profile_capabilities"]["private_companion_enabled"])
        self.assertEqual(1, host.delivery_bound)

    def test_configured_target_compatibility_migration_never_reopens_explicit_disable(self) -> None:
        host = _LegacyTargetMigrationHost()
        user: dict[str, Any] = {"proactive_daily_limit": 1}
        self.assertTrue(REQ036_CONFIGURED_TARGET_MIGRATION(host, "legacy-target", user))
        state = user["unified_profile_capabilities"]
        self.assertTrue(state["private_companion_enabled"])
        self.assertTrue(state["proactive_private_enabled"])
        self.assertEqual("legacy_configured_target_migration", state["grant_source"])
        update_capabilities(
            user,
            {"private_companion_enabled": False},
            actor_authorized=True,
            actor_id="page_administrator",
            target_identity="legacy-target",
        )
        self.assertFalse(REQ036_CONFIGURED_TARGET_MIGRATION(host, "legacy-target", user))
        self.assertFalse(user["unified_profile_capabilities"]["private_companion_enabled"])

    def test_user_list_deduplicates_resolved_person_sources_without_crashing(self) -> None:
        result = asyncio.run(REQ036_USER_LIST(_UserListHost()))
        self.assertEqual(1, result["total"])
        self.assertEqual("source-a", result["items"][0]["user_id"])
        self.assertEqual(["source-b"], result["items"][0]["alias_user_ids"])


if __name__ == "__main__":
    unittest.main()

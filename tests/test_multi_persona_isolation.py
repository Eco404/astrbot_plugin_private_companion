# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from quart import Quart

from astrbot_plugin_private_companion.main import (
    PrivateCompanionPlugin,
    _multi_persona_event_context,
)
from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


def _plugin_harness(root: str) -> PrivateCompanionPlugin:
    plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
    plugin.enable_multi_persona_mode = True
    plugin.multi_persona_primary_id = "main"
    plugin.multi_persona_ids = ["main", "alt"]
    plugin.plugin_specific_persona_id = "main"
    plugin.config = {
        "multi_persona_ids": ["main", "alt"],
        "multi_persona_window_bindings": {},
    }
    plugin._persona_profiles_dir = str(Path(root) / "persona_profiles")
    plugin._persona_data_profiles = {}
    plugin._persona_window_claims = {}
    plugin._persona_window_conflicts = {}
    plugin._page_current_persona_id = "main"
    plugin._data_lock = asyncio.Lock()
    plugin._stop_event = asyncio.Event()
    plugin._data_save_task = None
    plugin._data_save_dirty = False
    plugin._persona_data_save_tasks = {}
    plugin._persona_data_save_dirty = set()
    plugin._data_default = {
        "users": {"legacy": {"name": "旧用户"}},
        "daily_plan": {"marker": "旧日程"},
        "bot_diaries": {"2026-08-03": {"content": "旧日记"}},
        "persona_settings": {},
    }
    plugin._new_store = lambda: {
        "users": {},
        "daily_plan": {},
        "bot_diaries": {},
        "persona_settings": {},
    }
    plugin._ensure_store_defaults = lambda profile: profile
    plugin._sanitize_store_control_tags_inplace = lambda _profile: 0
    plugin._compact_store_history_inplace = lambda _profile: {}
    plugin._log_store_control_cleanup = lambda *_args, **_kwargs: None
    plugin._save_config_if_possible = AsyncMock(return_value=True)
    return plugin


@_multi_persona_event_context
async def _record_event_persona(plugin, event, delay: float = 0.01):
    active = plugin._active_persona_scope()
    plugin.data["event_trace"] = [active]
    await asyncio.sleep(delay)
    return plugin._active_persona_scope(), list(plugin.data["event_trace"])


class _ConversationManager:
    def __init__(self, persona_id: str) -> None:
        self.persona_id = persona_id

    async def get_curr_conversation_id(self, umo: str) -> str:
        return f"conversation:{umo}"

    async def get_conversation(self, umo: str, conversation_id: str):
        return SimpleNamespace(persona_id=self.persona_id)


class MultiPersonaIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_inherits_legacy_store_and_secondary_starts_blank(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)

            primary = plugin._ensure_persona_profile("main")
            secondary = plugin._ensure_persona_profile("alt")

            self.assertEqual("旧用户", primary["users"]["legacy"]["name"])
            self.assertEqual("旧日程", primary["daily_plan"]["marker"])
            self.assertEqual({}, secondary["users"])
            self.assertEqual({}, secondary["daily_plan"])
            self.assertEqual({}, secondary["bot_diaries"])

    async def test_private_and_group_events_keep_concurrent_profiles_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin.config["multi_persona_window_bindings"] = {
                "default:FriendMessage:10001": "main",
                "default:GroupMessage:20001": "alt",
            }
            private_event = SimpleNamespace(
                unified_msg_origin="default:FriendMessage:10001"
            )
            group_event = SimpleNamespace(
                unified_msg_origin="default:GroupMessage:20001"
            )

            private_result, group_result = await asyncio.gather(
                _record_event_persona(plugin, private_event, 0.02),
                _record_event_persona(plugin, group_event, 0.01),
            )

            self.assertEqual(("main", ["main"]), private_result)
            self.assertEqual(("alt", ["alt"]), group_result)
            self.assertEqual(["main"], plugin._persona_data_profiles["main"]["event_trace"])
            self.assertEqual(["alt"], plugin._persona_data_profiles["alt"]["event_trace"])
            self.assertEqual("", plugin._active_persona_scope())

    async def test_explicit_binding_wins_and_conversation_persona_is_auto_bound(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            explicit_umo = "default:FriendMessage:explicit"
            plugin.config["multi_persona_window_bindings"] = {explicit_umo: "main"}
            plugin.context = SimpleNamespace(
                conversation_manager=_ConversationManager("alt")
            )

            token, persona_id = await plugin._activate_persona_for_event_context(
                SimpleNamespace(unified_msg_origin=explicit_umo)
            )
            self.assertEqual("main", persona_id)
            plugin._deactivate_persona_for_event(token)

            auto_umo = "default:GroupMessage:auto"
            event = SimpleNamespace(unified_msg_origin=auto_umo)
            token, persona_id = await plugin._activate_persona_for_event_context(event)
            self.assertEqual("alt", persona_id)
            self.assertEqual("alt", event.private_companion_persona_id)
            self.assertEqual(
                "alt",
                plugin.config["multi_persona_window_bindings"][auto_umo],
            )
            plugin._deactivate_persona_for_event(token)
            plugin._save_config_if_possible.assert_awaited_once()

    async def test_conflict_switch_and_default_diary_migration(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            source = plugin._ensure_persona_profile("main")
            target = plugin._ensure_persona_profile("alt")
            target["runtime_cache"] = {"old": True}

            migrated = await plugin._migrate_persona_profile_async("main", "alt", [])

            self.assertTrue(migrated["ok"])
            self.assertIn("bot_diaries", migrated["keys"])
            self.assertEqual(source["bot_diaries"], target["bot_diaries"])
            self.assertNotIn("runtime_cache", target)
            stored = (Path(root) / "persona_profiles" / "alt.json").read_text(
                encoding="utf-8"
            )
            self.assertIn("旧日记", stored)

            window = "default:FriendMessage:conflict"
            plugin.config["multi_persona_window_bindings"] = {window: "main"}
            conflict = plugin._switch_persona_for_window("alt", window_key=window)
            self.assertTrue(conflict["conflict"])
            self.assertEqual("main", plugin._persona_window_bindings()[window])

            switched = plugin._switch_persona_for_window(
                "alt", window_key=window, force=True
            )
            self.assertTrue(switched["switched"])
            self.assertEqual("alt", plugin._persona_window_bindings()[window])

    async def test_page_route_reads_selected_persona_users_and_schedule(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin._ensure_persona_profile("main")
            alt = plugin._ensure_persona_profile("alt")
            alt["users"] = {"alt-user": {"name": "次人格用户"}}
            alt["daily_plan"] = {"marker": "次人格日程"}
            api = PrivateCompanionPageApi(plugin)
            app = Quart(__name__)

            async def read_profile():
                return {
                    "persona": plugin._active_persona_scope(),
                    "users": list(plugin.data["users"]),
                    "schedule": plugin.data["daily_plan"]["marker"],
                }

            handler = api._persona_scoped_route_handler(read_profile)
            async with app.test_request_context("/?_persona_id=main"):
                main_payload = await handler()
            async with app.test_request_context("/?_persona_id=alt"):
                alt_payload = await handler()

            self.assertEqual(
                {"persona": "main", "users": ["legacy"], "schedule": "旧日程"},
                main_payload,
            )
            self.assertEqual(
                {
                    "persona": "alt",
                    "users": ["alt-user"],
                    "schedule": "次人格日程",
                },
                alt_payload,
            )
            self.assertEqual("", plugin._active_persona_scope())

    async def test_scheduler_runs_each_persona_and_uses_effective_proactive_persona(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            seen: list[str] = []

            async def tick():
                seen.append(plugin._active_persona_scope())

            plugin._tick = tick
            plugin._scheduler_maintenance_tasks = lambda: ()
            await plugin._run_scheduler_cycle()

            self.assertEqual(["main", "alt"], seen)
            token = plugin._activate_persona_id("alt")
            try:
                original = SimpleNamespace(persona_id="main", marker="original")
                scoped = plugin._proactive_conversation_with_configured_persona(original)
            finally:
                plugin._deactivate_persona_for_event(token)
            self.assertEqual("alt", scoped.persona_id)
            self.assertEqual("main", original.persona_id)
            self.assertIsNot(scoped, original)

    async def test_disabled_mode_keeps_single_profile_behavior(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin.enable_multi_persona_mode = False
            plugin.plugin_specific_persona_id = "single-persona"

            self.assertIs(plugin.data, plugin._data_default)
            self.assertIsNone(plugin._activate_persona_id("alt"))
            self.assertEqual("single-persona", plugin._effective_plugin_persona_id())
            self.assertEqual([""], plugin._scheduler_persona_ids())

    async def test_string_persona_config_splits_whitespace_and_punctuation(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin.config["multi_persona_ids"] = "main sister,alt，work、night"

            self.assertEqual(
                ["main", "sister", "alt", "work", "night"],
                plugin._configured_multi_persona_ids(),
            )

    async def test_unicode_persona_ids_keep_their_full_logical_identity(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)

            self.assertEqual("星缘-私聊", plugin._sanitize_persona_id("星缘-私聊"))
            self.assertEqual(
                "中文 Persona V2",
                plugin._sanitize_persona_id("中文 Persona V2"),
            )
            self.assertEqual("姐姐人格", plugin._sanitize_persona_id("姐\n姐人格"))
            joined_name = "星缘\u200dAI"
            self.assertEqual(joined_name, plugin._sanitize_persona_id(joined_name))
            self.assertEqual(
                joined_name,
                plugin._persona_id_from_profile_path(
                    plugin._persona_profile_path(joined_name)
                ),
            )

    async def test_unicode_persona_profile_round_trips_and_is_enumerated(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            persona_id = "星缘-私聊"
            plugin.multi_persona_primary_id = persona_id
            plugin.multi_persona_ids = [persona_id, "alt"]
            plugin.config["multi_persona_ids"] = [persona_id, "alt"]

            profile = plugin._ensure_persona_profile(persona_id)
            profile["unicode_marker"] = "中文资料"
            plugin._save_persona_profile_sync(persona_id)

            path = Path(root) / "persona_profiles" / "星缘-私聊.json"
            self.assertTrue(path.is_file())
            plugin._persona_data_profiles.clear()
            self.assertEqual(
                "中文资料",
                plugin._ensure_persona_profile(persona_id)["unicode_marker"],
            )
            self.assertIn(persona_id, plugin._persona_profile_ids())

            window = "default:FriendMessage:unicode-persona"
            switched = plugin._switch_persona_for_window(
                persona_id,
                window_key=window,
            )
            self.assertTrue(switched["switched"])
            self.assertEqual(persona_id, switched["persona_id"])
            self.assertEqual(
                persona_id,
                plugin.config["multi_persona_window_bindings"][window],
            )

    async def test_profile_filename_encoding_is_safe_and_reversible(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            persona_id = "../姐姐:主/人格%?"
            plugin.config["multi_persona_ids"] = ["main", persona_id]

            path = plugin._persona_profile_path(persona_id)
            self.assertEqual(
                (Path(root) / "persona_profiles").resolve(),
                path.parent.resolve(),
            )
            self.assertNotIn("/", path.name)
            self.assertNotIn(":", path.name)
            self.assertNotIn("?", path.name)
            plugin._ensure_persona_profile(persona_id)["safe_marker"] = True
            plugin._save_persona_profile_sync(persona_id)

            plugin._persona_data_profiles.clear()
            self.assertTrue(plugin._ensure_persona_profile(persona_id)["safe_marker"])
            self.assertIn(persona_id, plugin._persona_profile_ids())

    async def test_legacy_ascii_profile_filenames_remain_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)

            self.assertEqual("main.json", plugin._persona_profile_path("main").name)
            self.assertEqual("alt.json", plugin._persona_profile_path("alt").name)
            self.assertNotEqual("CON.json", plugin._persona_profile_path("CON").name)
            self.assertEqual(
                "CON",
                plugin._persona_id_from_profile_path(plugin._persona_profile_path("CON")),
            )

    async def test_removed_profile_is_recoverable_but_not_scheduled_or_bound(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin._ensure_persona_profile("alt")["users"] = {
                "old": {"umo": "default:FriendMessage:old"}
            }
            plugin._save_persona_profile_sync("alt")
            plugin.config["multi_persona_ids"] = ["main"]
            stale_window = "default:FriendMessage:stale"
            plugin.config["multi_persona_window_bindings"] = {stale_window: "alt"}

            self.assertIn("alt", plugin._persona_profile_ids())
            self.assertEqual(["main"], plugin._scheduler_persona_ids())
            self.assertIsNone(plugin._activate_persona_id("alt"))
            recovery_token = plugin._activate_persona_id("alt", allow_inactive=True)
            try:
                self.assertEqual(["main"], plugin._scheduler_persona_ids())
                self.assertIn("old", plugin.data["users"])
            finally:
                plugin._deactivate_persona_for_event(recovery_token)
            self.assertEqual(
                "main",
                plugin._persona_id_for_event(
                    SimpleNamespace(unified_msg_origin=stale_window)
                )[0],
            )
            event = SimpleNamespace(unified_msg_origin=stale_window)
            token, activated = await plugin._activate_persona_for_event_context(event)
            try:
                self.assertEqual("main", activated)
                self.assertEqual("main", event.private_companion_persona_id)
            finally:
                plugin._deactivate_persona_for_event(token)

            seen: list[str] = []

            async def record_state():
                seen.append(plugin._active_persona_scope())

            plugin._ensure_daily_state = record_state
            plugin._ensure_daily_plan = AsyncMock()
            plugin._ensure_daily_diary = AsyncMock()
            plugin._maybe_settle_skill_growth = AsyncMock()
            await plugin._startup_prepare_today()
            self.assertEqual(["main"], seen)

    async def test_startup_failure_in_one_persona_does_not_skip_the_next(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            seen: list[str] = []

            async def ensure_state():
                persona_id = plugin._active_persona_scope()
                seen.append(persona_id)
                if persona_id == "main":
                    raise RuntimeError("main startup failed")

            plugin._ensure_daily_state = ensure_state
            plugin._ensure_daily_plan = AsyncMock()
            plugin._ensure_daily_diary = AsyncMock()
            plugin._maybe_settle_skill_growth = AsyncMock()

            await plugin._startup_prepare_today()

            self.assertEqual(["main", "alt"], seen)
            plugin._ensure_daily_plan.assert_awaited_once()
            plugin._ensure_daily_diary.assert_awaited_once()
            plugin._maybe_settle_skill_growth.assert_awaited_once()

    async def test_delayed_saves_persist_each_persona_independently(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)

            main_token = plugin._activate_persona_id("main")
            try:
                plugin.data["save_marker"] = "main"
                plugin._save_data_sync()
            finally:
                plugin._deactivate_persona_for_event(main_token)

            alt_token = plugin._activate_persona_id("alt")
            try:
                plugin.data["save_marker"] = "alt"
                plugin._save_data_sync()
            finally:
                plugin._deactivate_persona_for_event(alt_token)

            await plugin._flush_scheduled_data_save()

            main = (Path(root) / "persona_profiles" / "main.json").read_text(
                encoding="utf-8"
            )
            alt = (Path(root) / "persona_profiles" / "alt.json").read_text(
                encoding="utf-8"
            )
            self.assertIn('"save_marker": "main"', main)
            self.assertIn('"save_marker": "alt"', alt)
            self.assertFalse(plugin._persona_data_save_dirty)
            self.assertEqual({}, plugin._persona_data_save_tasks)

    async def test_terminate_waits_for_inflight_persona_writer_before_final_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            original_writer = plugin._write_persona_data_snapshot_sync
            writer_started = threading.Event()
            writer_release = threading.Event()
            first_write = True

            def blocking_writer(persona_id, snapshot):
                nonlocal first_write
                if first_write:
                    first_write = False
                    writer_started.set()
                    writer_release.wait(timeout=3)
                return original_writer(persona_id, snapshot)

            plugin._write_persona_data_snapshot_sync = blocking_writer
            plugin._write_data_snapshot_sync = lambda _snapshot: 0
            token = plugin._activate_persona_id("main")
            try:
                plugin.data["final_marker"] = "old"
                plugin._schedule_data_save(delay=0.0)
            finally:
                plugin._deactivate_persona_for_event(token)
            started = await asyncio.to_thread(writer_started.wait, 1.0)
            self.assertTrue(started)

            token = plugin._activate_persona_id("main")
            try:
                plugin.data["final_marker"] = "latest"
            finally:
                plugin._deactivate_persona_for_event(token)
            plugin._stop_event.set()
            final_save = asyncio.create_task(plugin._save_data_on_terminate())
            await asyncio.sleep(0.05)
            self.assertFalse(final_save.done())
            writer_release.set()
            await asyncio.wait_for(final_save, timeout=2.0)

            stored = (Path(root) / "persona_profiles" / "main.json").read_text(
                encoding="utf-8"
            )
            self.assertIn('"final_marker": "latest"', stored)

    async def test_terminate_saves_all_loaded_persona_profiles(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin._ensure_persona_profile("main")["final_marker"] = "main"
            plugin._ensure_persona_profile("alt")["final_marker"] = "alt"
            plugin._write_data_snapshot_sync = lambda _snapshot: 0

            await plugin._save_data_on_terminate()

            for persona_id in ("main", "alt"):
                stored = (
                    Path(root) / "persona_profiles" / f"{persona_id}.json"
                ).read_text(encoding="utf-8")
                self.assertIn(f'"final_marker": "{persona_id}"', stored)

    async def test_force_state_results_do_not_cross_persona_scopes(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin._daily_state_generation_lock = asyncio.Lock()
            started = asyncio.Event()
            release = asyncio.Event()
            calls: list[str] = []

            async def generate_state(**_kwargs):
                persona_id = plugin._active_persona_scope()
                calls.append(persona_id)
                if persona_id == "main":
                    started.set()
                    await release.wait()
                return {"persona_id": persona_id}

            plugin._ensure_daily_state_once = generate_state
            main_token = plugin._activate_persona_id("main")
            try:
                main_task = asyncio.create_task(plugin._ensure_daily_state(force=True))
            finally:
                plugin._deactivate_persona_for_event(main_token)
            await started.wait()
            alt_token = plugin._activate_persona_id("alt")
            try:
                alt_task = asyncio.create_task(plugin._ensure_daily_state(force=True))
            finally:
                plugin._deactivate_persona_for_event(alt_token)
            alt_result = await asyncio.wait_for(alt_task, timeout=0.5)
            self.assertFalse(main_task.done())
            release.set()
            main_result = await asyncio.wait_for(main_task, timeout=0.5)
            self.assertEqual(["main", "alt"], calls)
            self.assertEqual("main", main_result["persona_id"])
            self.assertEqual("alt", alt_result["persona_id"])

    async def test_force_diary_results_do_not_cross_persona_scopes(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = _plugin_harness(root)
            plugin._daily_diary_generation_lock = asyncio.Lock()
            started = asyncio.Event()
            release = asyncio.Event()
            calls: list[str] = []

            async def generate_diary(*, force=False):
                del force
                persona_id = plugin._active_persona_scope()
                calls.append(persona_id)
                if persona_id == "main":
                    started.set()
                    await release.wait()
                return {"persona_id": persona_id}

            plugin._ensure_daily_diary_once = generate_diary
            main_token = plugin._activate_persona_id("main")
            try:
                main_task = asyncio.create_task(plugin._ensure_daily_diary(force=True))
            finally:
                plugin._deactivate_persona_for_event(main_token)
            await started.wait()
            alt_token = plugin._activate_persona_id("alt")
            try:
                alt_task = asyncio.create_task(plugin._ensure_daily_diary(force=True))
            finally:
                plugin._deactivate_persona_for_event(alt_token)
            alt_result = await asyncio.wait_for(alt_task, timeout=0.5)
            self.assertFalse(main_task.done())
            release.set()
            main_result = await asyncio.wait_for(main_task, timeout=0.5)
            self.assertEqual(["main", "alt"], calls)
            self.assertEqual("main", main_result["persona_id"])
            self.assertEqual("alt", alt_result["persona_id"])

    async def test_all_astrbot_filter_handlers_bind_persona_context(self):
        source_path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        filter_handlers: list[tuple[str, list[str]]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(item) for item in node.decorator_list]
            if any(item.startswith("filter.") for item in decorators):
                filter_handlers.append((node.name, decorators))

        self.assertGreaterEqual(len(filter_handlers), 70)
        missing = [
            name
            for name, decorators in filter_handlers
            if "_multi_persona_event_context" not in decorators
        ]
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()

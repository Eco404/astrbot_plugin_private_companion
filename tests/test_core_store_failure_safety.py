# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_private_companion.core_store import CoreStoreMixin


class _AsyncConfig:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.awaited = False

    async def save_config(self) -> None:
        await asyncio.sleep(0)
        self.awaited = True
        if self.fail:
            raise OSError("配置目录不可写")


class _CoreHarness(CoreStoreMixin):
    def __init__(self) -> None:
        self.config = _AsyncConfig()
        self.store_manager = None
        self._data_save_task = None
        self._data_save_dirty = False
        self._stop_event = asyncio.Event()

    @staticmethod
    def _new_store() -> dict:
        return {"users": {}}

    def _configured_target_ids(self) -> list[str]:
        return ["owner"]

    def _is_bot_self_user_id(self, _user_id: str) -> bool:
        return False


class _StartupHarness(CoreStoreMixin):
    def __init__(self) -> None:
        self.data = {"bot_diaries": []}
        self.diary_calls: list[dict[str, object]] = []

    async def _ensure_daily_state(self) -> None:
        return None

    async def _ensure_daily_plan(self) -> None:
        return None

    async def _ensure_daily_diary(self, **kwargs) -> None:
        self.diary_calls.append(dict(kwargs))

    async def _maybe_settle_skill_growth(self) -> None:
        return None


class CoreStoreFailureSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_diary_check_does_not_force_generation(self) -> None:
        harness = _StartupHarness()

        await harness._startup_prepare_today()

        self.assertEqual(harness.diary_calls, [{}])

    async def test_async_config_save_is_awaited(self) -> None:
        harness = _CoreHarness()

        saved = await harness._save_config_if_possible()

        self.assertTrue(saved)
        self.assertTrue(harness.config.awaited)

    async def test_async_config_save_failure_is_reported(self) -> None:
        harness = _CoreHarness()
        harness.config = _AsyncConfig(fail=True)

        saved = await harness._save_config_if_possible()

        self.assertFalse(saved)
        self.assertTrue(harness.config.awaited)

    async def test_flush_does_not_reschedule_dirty_write_while_stopping(self) -> None:
        harness = _CoreHarness()
        harness._data_save_dirty = True
        harness._stop_event.set()

        await asyncio.wait_for(harness._flush_scheduled_data_save(), timeout=0.2)

        self.assertIsNone(harness._data_save_task)
        self.assertTrue(harness._data_save_dirty)

    def test_store_manager_failure_does_not_fall_back_to_stale_json(self) -> None:
        harness = _CoreHarness()
        harness.store_manager = SimpleNamespace(
            load_initial_store=lambda: (_ for _ in ()).throw(OSError("database is locked"))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "companions.json"
            data_file.write_text('{"users": {"42": {"name": "stale"}}}', encoding="utf-8")
            harness.data_file = str(data_file)

            with self.assertRaisesRegex(OSError, "database is locked"):
                harness._load_data_sync()

    def test_existing_invalid_direct_json_is_not_replaced_with_defaults(self) -> None:
        harness = _CoreHarness()
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "companions.json"
            data_file.write_text('{"users": ', encoding="utf-8")
            harness.data_file = str(data_file)

            with self.assertRaises(Exception):
                harness._load_data_sync()

            self.assertEqual('{"users": ', data_file.read_text(encoding="utf-8"))

    def test_group_only_placeholders_are_removed_from_private_users(self) -> None:
        harness = _CoreHarness()
        harness.default_nickname = "主要用户昵称"
        harness.default_style = "默认语气"
        harness.data = {
            "users": {
                "owner": {"user_id": "owner", "nickname": "主要用户昵称"},
                "group_sender": {
                    "user_id": "group_sender",
                    "nickname": "主要用户昵称",
                    "style": "默认语气",
                    "enabled": False,
                    "relationship_role": "friend",
                    "recent_group_messages": [{"group_id": "100", "text": "群消息"}],
                    "reaction_expression": {"last_sent_at": 12},
                    "last_inbound_umo": "default:GroupMessage:100",
                },
            }
        }

        changed = harness._cleanup_orphan_reaction_expression_users()

        self.assertTrue(changed)
        self.assertEqual(["owner"], list(harness.data["users"]))

    def test_private_activity_and_manual_records_survive_orphan_cleanup(self) -> None:
        harness = _CoreHarness()
        harness.default_nickname = "主要用户昵称"
        harness.default_style = "默认语气"
        harness.data = {
            "users": {
                "private": {
                    "user_id": "private",
                    "nickname": "主要用户昵称",
                    "style": "默认语气",
                    "enabled": False,
                    "relationship_role": "friend",
                    "last_private_seen": 10,
                },
                "manual": {
                    "user_id": "manual",
                    "nickname": "主要用户昵称",
                    "style": "默认语气",
                    "enabled": False,
                    "manual_disabled": True,
                    "relationship_role": "friend",
                },
                "profiled": {
                    "user_id": "profiled",
                    "nickname": "独立昵称",
                    "style": "默认语气",
                    "enabled": False,
                    "relationship_role": "friend",
                },
            }
        }

        changed = harness._cleanup_orphan_reaction_expression_users()

        self.assertFalse(changed)
        self.assertEqual({"private", "manual", "profiled"}, set(harness.data["users"]))


if __name__ == "__main__":
    unittest.main()

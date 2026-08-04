# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from quart import Quart

from astrbot_plugin_private_companion.page_api import (
    BOOKSHELF_ACCESS_TOKEN_TTL_SECONDS,
    PrivateCompanionPageApi,
)
from astrbot_plugin_private_companion.private_reading import PrivateReadingMixin


class _BookshelfAccessHarness(PrivateReadingMixin):
    def __init__(self, root: Path) -> None:
        self.data_dir = str(root)
        self.data = {
            "bookshelf_secret": {"password": "2468", "basis": "manual"},
            "bookshelf_items": [],
            "creative_projects": [],
            "bot_diaries": [],
            "memo_notes": [],
            "jm_cosmos_integration": {},
        }
        self._data_lock = asyncio.Lock()
        self.save_calls = 0
        self.enable_multi_persona_mode = False
        self.active_persona_id = ""
        self._page_current_persona_id = ""

    def _save_data_sync(self) -> None:
        self.save_calls += 1

    def _active_persona_scope(self) -> str:
        return self.active_persona_id

    @staticmethod
    def _format_timestamp_elapsed(_value: object) -> str:
        return "刚刚"

    @staticmethod
    def _polish_diary_text(value: object, *, field: str = "") -> str:
        return str(value or "")


class BookshelfAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ephemeral_issue_uses_24_hour_ttl_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            api = PrivateCompanionPageApi(harness)
            started = time.time()

            token = api._issue_bookshelf_access_token()

            expires_at = api._bookshelf_access_token_expires_at(token)
            self.assertGreaterEqual(expires_at, started + BOOKSHELF_ACCESS_TOKEN_TTL_SECONDS - 1)
            self.assertLessEqual(expires_at, started + BOOKSHELF_ACCESS_TOKEN_TTL_SECONDS + 1)
            self.assertNotIn("web_access", harness.data["bookshelf_secret"])

    async def test_unlock_persists_only_token_hash_and_session_restores_after_new_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            api = PrivateCompanionPageApi(harness)
            app = Quart(__name__)

            async with app.test_request_context("/bookshelf/unlock", method="POST", json={"password": "2468"}):
                unlocked = await api.unlock_bookshelf()

            self.assertTrue(unlocked["success"])
            bookshelf = unlocked["data"]["bookshelf"]
            token = bookshelf["access_token"]
            persisted = harness.data["bookshelf_secret"]["web_access"]
            self.assertNotIn(token, str(persisted))
            self.assertEqual(len(persisted["tokens"]), 1)
            self.assertGreaterEqual(bookshelf["access_expires_at"], int(time.time()) + 86390)

            restored_api = PrivateCompanionPageApi(harness)
            restored_api._bookshelf_summary = AsyncMock(
                return_value={"unlocked": True, "access_token": token}
            )
            async with app.test_request_context(f"/bookshelf/session?access_token={token}"):
                restored = await restored_api.get_bookshelf_session()

            self.assertTrue(restored["success"])
            self.assertEqual(restored["data"]["bookshelf"]["access_token"], token)
            restored_api._bookshelf_summary.assert_awaited_once()

    async def test_expired_persisted_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            api = PrivateCompanionPageApi(harness)
            token = api._issue_bookshelf_access_token(persist=True)
            harness.data["bookshelf_secret"]["web_access"]["tokens"][0]["expires_at"] = time.time() - 1
            restored_api = PrivateCompanionPageApi(harness)

            self.assertFalse(restored_api._bookshelf_access_token_valid(token))

    async def test_bookshelf_token_is_bound_to_issuing_persona(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.enable_multi_persona_mode = True
            harness.active_persona_id = "persona-a"
            harness._page_current_persona_id = "persona-a"
            api = PrivateCompanionPageApi(harness)

            runtime_token = api._issue_bookshelf_access_token()
            persisted_token = api._issue_bookshelf_access_token(persist=True)
            persisted = harness.data["bookshelf_secret"]["web_access"]["tokens"][0]
            self.assertEqual("persona-a", persisted["persona_id"])

            harness.active_persona_id = "persona-b"
            harness._page_current_persona_id = "persona-b"
            self.assertFalse(api._bookshelf_access_token_valid(runtime_token))
            self.assertFalse(api._bookshelf_access_token_valid(persisted_token))

            delattr(harness, "_bookshelf_access_tokens")
            restored_api = PrivateCompanionPageApi(harness)
            self.assertFalse(restored_api._bookshelf_access_token_valid(persisted_token))
            harness.active_persona_id = "persona-a"
            harness._page_current_persona_id = "persona-a"
            self.assertTrue(restored_api._bookshelf_access_token_valid(persisted_token))

    async def test_single_diary_entry_can_be_deleted_and_is_tombstoned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.data["bot_diaries"] = [
                {"date": "2026-08-04", "body": "只留下了这一天。"},
            ]
            harness.data["diary_generated_day"] = "2026-08-04"
            api = PrivateCompanionPageApi(harness)
            api._bookshelf_summary = AsyncMock(return_value={"secret_books": []})
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)

            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={"kind": "diary", "date": "2026-08-04", "access_token": token},
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["success"])
            self.assertTrue(result["data"]["changed"])
            self.assertEqual([], harness.data["bot_diaries"])
            self.assertEqual(["2026-08-04"], harness.data["daily_diary_deleted_days"])
            self.assertEqual(1, harness.data["daily_diary_delete_revision"])
            self.assertEqual(1, harness.save_calls)

    async def test_diary_delete_normalizes_timestamp_and_preserves_other_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.data["bot_diaries"] = [
                {"date": "2026-08-03", "body": "前一天"},
                {"date": "2026-08-04T00:12:00+08:00", "body": "要删除"},
            ]
            api = PrivateCompanionPageApi(harness)
            api._bookshelf_summary = AsyncMock(return_value={"secret_books": []})
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)

            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={"kind": "diary", "date": "2026-08-04", "access_token": token},
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["data"]["changed"])
            self.assertEqual(["2026-08-03"], [item["date"] for item in harness.data["bot_diaries"]])

    async def test_entry_key_deletes_only_selected_diary_when_dates_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.data["bot_diaries"] = [
                {"date": "2026-08-04", "body": "第一篇"},
                {"date": "2026-08-04", "body": "第二篇"},
            ]
            api = PrivateCompanionPageApi(harness)
            entries = api._bookshelf_diary_entries(harness.data["bot_diaries"])
            self.assertNotEqual(entries[0]["entry_key"], entries[1]["entry_key"])

            api._bookshelf_summary = AsyncMock(return_value={"secret_books": []})
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)
            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={
                    "kind": "diary",
                    "date": "2026-08-04",
                    "entry_key": entries[0]["entry_key"],
                    "access_token": token,
                },
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["data"]["changed"])
            self.assertEqual(["第二篇"], [item["body"] for item in harness.data["bot_diaries"]])

    async def test_entry_key_deletes_only_one_completely_identical_diary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.data["bot_diaries"] = [
                {"date": "2026-08-04", "body": "完全相同"},
                {"date": "2026-08-04", "body": "完全相同"},
                {"date": "2026-08-03", "body": "保留的前一天"},
            ]
            api = PrivateCompanionPageApi(harness)
            entries = api._bookshelf_diary_entries(harness.data["bot_diaries"])
            repeated_entries = [item for item in entries if item["body"] == "完全相同"]
            self.assertNotEqual(repeated_entries[0]["entry_key"], repeated_entries[1]["entry_key"])
            self.assertEqual(
                [item["entry_key"] for item in entries],
                [
                    item["entry_key"]
                    for item in api._bookshelf_diary_entries(harness.data["bot_diaries"])
                ],
            )

            api._bookshelf_summary = AsyncMock(return_value={"secret_books": []})
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)
            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={
                    "kind": "diary",
                    "date": "2026-08-04",
                    "entry_key": repeated_entries[1]["entry_key"],
                    "access_token": token,
                },
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["data"]["changed"])
            self.assertEqual(2, len(harness.data["bot_diaries"]))
            self.assertEqual(
                ["完全相同", "保留的前一天"],
                [item["body"] for item in harness.data["bot_diaries"]],
            )

    async def test_legacy_dictionary_diary_can_be_listed_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.data["bot_diaries"] = {
                "2026/8/3": {"content": "旧格式日记"},
                "2026-08-04": {"body": "保留"},
            }
            api = PrivateCompanionPageApi(harness)
            entries = api._bookshelf_diary_entries(harness.data["bot_diaries"])
            self.assertEqual(["2026-08-03", "2026-08-04"], [item["date"] for item in entries])
            self.assertEqual("旧格式日记", entries[0]["body"])

            api._bookshelf_summary = AsyncMock(return_value={"secret_books": []})
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)
            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={"kind": "diary", "date": "2026-08-03", "access_token": token},
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["data"]["changed"])
            self.assertEqual(["2026-08-04"], list(harness.data["bot_diaries"]))

    async def test_missing_date_diary_uses_stable_entry_key_for_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _BookshelfAccessHarness(Path(directory))
            harness.data["bot_diaries"] = [
                {"body": "没有日期的旧日记"},
                {"date": "2026-08-04", "body": "保留"},
            ]
            api = PrivateCompanionPageApi(harness)
            entries = api._bookshelf_diary_entries(harness.data["bot_diaries"])
            missing = next(item for item in entries if item["date"] == "某天")
            self.assertRegex(missing["entry_key"], r"^diary:[0-9a-f]{24}$")

            api._bookshelf_summary = AsyncMock(return_value={"secret_books": []})
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)
            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={
                    "kind": "diary",
                    "date": "某天",
                    "entry_key": missing["entry_key"],
                    "access_token": token,
                },
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["data"]["changed"])
            self.assertEqual(["2026-08-04"], [item.get("date") for item in harness.data["bot_diaries"]])


if __name__ == "__main__":
    unittest.main()

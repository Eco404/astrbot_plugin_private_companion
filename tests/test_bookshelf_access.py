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

    def _save_data_sync(self) -> None:
        self.save_calls += 1

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


if __name__ == "__main__":
    unittest.main()

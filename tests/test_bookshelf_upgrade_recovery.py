# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quart import Quart

from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi
from astrbot_plugin_private_companion.private_reading import PrivateReadingMixin


class _BookshelfHarness(PrivateReadingMixin):
    def __init__(self, root: Path, data: dict | None = None) -> None:
        self.data_dir = str(root)
        self.data = data if isinstance(data, dict) else {}
        self._data_lock = asyncio.Lock()
        self.save_calls = 0

    def _save_data_sync(self) -> None:
        self.save_calls += 1

    @staticmethod
    def _format_timestamp_elapsed(_value) -> str:
        return "刚刚"

    @staticmethod
    def _polish_diary_text(value, *, field: str = "") -> str:
        return str(value or "")


class BookshelfUpgradeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_pages(root: Path, album_id: str, count: int = 2) -> Path:
        album_root = root / "bookshelf_pages" / album_id
        album_root.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (album_root / f"{index:04d}.jpg").write_bytes(f"page-{index}".encode("utf-8"))
        return album_root

    async def test_missing_store_items_are_recovered_from_local_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_pages(root, "1001", 3)
            data = {
                "bookshelf_items": [],
                "jm_cosmos_integration": {
                    "preference_profile": {
                        "history": [
                            {
                                "album_id": "1001",
                                "title": "旧版书名",
                                "terms": ["测试"],
                                "bot_rating": 8,
                            }
                        ]
                    }
                },
                "bookshelf_store_revision": 0,
            }
            harness = _BookshelfHarness(root, data)

            recovered = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(recovered, 1)
            self.assertEqual(len(data["bookshelf_items"]), 1)
            self.assertEqual(data["bookshelf_items"][0]["title"], "旧版书名")
            self.assertEqual(len(data["bookshelf_items"][0]["pages"]), 3)
            self.assertGreater(data["bookshelf_store_revision"], 0)

    async def test_deleted_album_directory_is_not_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_pages(root, "2002")
            data = {
                "bookshelf_items": [],
                "jm_cosmos_integration": {"deleted_album_ids": ["2002"]},
            }
            harness = _BookshelfHarness(root, data)

            recovered = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(recovered, 0)
            self.assertEqual(data["bookshelf_items"], [])

    async def test_existing_tombstoned_item_is_removed_during_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = {
                "bookshelf_items": [
                    {
                        "type": "jm_album",
                        "album_id": "dead",
                        "title": "已经删除",
                        "pages": [],
                    }
                ],
                "jm_cosmos_integration": {"deleted_album_ids": ["dead"]},
                "bookshelf_store_revision": 3,
            }
            harness = _BookshelfHarness(Path(directory), data)

            changed = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(changed, 1)
            self.assertEqual(data["bookshelf_items"], [])
            self.assertGreater(data["bookshelf_store_revision"], 3)

    async def test_jm_tombstone_preserves_explicit_non_jm_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manual_item = {
                "type": "manual_album",
                "album_id": "same",
                "title": "手工相册",
                "pages": [],
            }
            data = {
                "bookshelf_items": [manual_item],
                "jm_cosmos_integration": {"deleted_album_ids": ["same"]},
                "bookshelf_store_revision": 4,
            }
            harness = _BookshelfHarness(Path(directory), data)

            changed = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(changed, 0)
            self.assertEqual(data["bookshelf_items"], [manual_item])
            self.assertEqual(data["bookshelf_store_revision"], 4)

    async def test_scan_failure_still_persists_tombstone_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bookshelf_pages").mkdir()
            data = {
                "bookshelf_items": [
                    {"type": "jm_album", "album_id": "dead", "pages": []}
                ],
                "jm_cosmos_integration": {"deleted_album_ids": ["dead"]},
                "bookshelf_store_revision": 7,
            }
            harness = _BookshelfHarness(root, data)

            with patch.object(Path, "iterdir", side_effect=OSError("scan failed")):
                changed = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(changed, 1)
            self.assertEqual(data["bookshelf_items"], [])
            self.assertGreater(data["bookshelf_store_revision"], 7)

    async def test_existing_item_with_stale_paths_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_root = self._write_pages(root, "3003", 2)
            data = {
                "bookshelf_items": [
                    {
                        "type": "jm_album",
                        "album_id": "3003",
                        "title": "保留标题",
                        "pages": [{"index": 1, "path": "D:/old-location/0001.jpg"}],
                    }
                ],
                "jm_cosmos_integration": {},
            }
            harness = _BookshelfHarness(root, data)

            recovered = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(recovered, 1)
            self.assertEqual(data["bookshelf_items"][0]["title"], "保留标题")
            self.assertEqual(
                [Path(page["path"]) for page in data["bookshelf_items"][0]["pages"]],
                [album_root / "0001.jpg", album_root / "0002.jpg"],
            )

    async def test_invalid_bookshelf_structure_is_never_replaced_with_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_pages(root, "4004")
            data = {"bookshelf_items": {"legacy": "unexpected"}, "jm_cosmos_integration": {}}
            harness = _BookshelfHarness(root, data)

            recovered = harness._recover_bookshelf_items_from_local_pages_inplace(data)

            self.assertEqual(recovered, 0)
            self.assertEqual(data["bookshelf_items"], {"legacy": "unexpected"})

    async def test_unlocked_summary_returns_all_retained_books(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [
                {
                    "type": "jm_album",
                    "album_id": str(5000 + index),
                    "title": f"书-{index}",
                    "pages": [],
                    "created_ts": float(index),
                }
                for index in range(24)
            ]
            data = {
                "bookshelf_items": items,
                "jm_cosmos_integration": {},
                "bookshelf_secret": {},
                "creative_projects": [],
                "bot_diaries": [],
                "memo_notes": [],
            }
            harness = _BookshelfHarness(root, data)
            api = PrivateCompanionPageApi(harness)

            summary = await api._bookshelf_summary(data, unlocked=True)

            self.assertEqual(summary["jm_album_count"], 24)
            self.assertEqual(len(summary["secret_books"]), 24)

    async def test_tombstoned_item_and_last_album_are_hidden_from_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            deleted = {
                "type": "jm_album",
                "album_id": "dead",
                "title": "已经删除",
                "pages": [],
            }
            data = {
                "bookshelf_items": [deleted],
                "jm_cosmos_integration": {
                    "deleted_album_ids": ["dead"],
                    "last_album": dict(deleted),
                },
                "bookshelf_secret": {},
                "creative_projects": [],
                "bot_diaries": [],
                "memo_notes": [],
            }
            harness = _BookshelfHarness(Path(directory), data)
            api = PrivateCompanionPageApi(harness)

            overview = api._jm_cosmos_summary(data)
            summary = await api._bookshelf_summary(data, unlocked=True)

            self.assertEqual(summary["jm_album_count"], 0)
            self.assertEqual(summary["secret_count"], 0)
            self.assertEqual(summary["secret_books"], [])
            self.assertEqual(data["jm_cosmos_integration"]["last_album"], {})
            self.assertEqual(overview["last_album"]["id"], "")
            self.assertEqual(overview["last_album"]["title"], "")

    async def test_tombstoned_item_is_not_exposed_to_reply_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = {
                "bookshelf_items": [
                    {
                        "type": "jm_album",
                        "album_id": "dead",
                        "title": "已经删除",
                        "pages": [],
                    }
                ],
                "jm_cosmos_integration": {"deleted_album_ids": ["dead"]},
            }
            harness = _BookshelfHarness(Path(directory), data)
            harness.enable_jm_cosmos_integration = True

            context = harness._format_bookshelf_reading_context_for_reply("你最近看过什么")

            self.assertEqual(context, "")

    async def test_non_jm_item_is_not_hidden_or_rendered_as_private_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manual_item = {
                "type": "manual_album",
                "album_id": "same",
                "title": "手工相册",
                "pages": [],
            }
            data = {
                "bookshelf_items": [manual_item],
                "jm_cosmos_integration": {"deleted_album_ids": ["same"]},
                "bookshelf_secret": {},
                "creative_projects": [],
                "bot_diaries": [],
                "memo_notes": [],
            }
            harness = _BookshelfHarness(Path(directory), data)
            api = PrivateCompanionPageApi(harness)

            summary = await api._bookshelf_summary(data, unlocked=True)

            self.assertEqual(data["bookshelf_items"], [manual_item])
            self.assertEqual(summary["jm_album_count"], 0)
            self.assertEqual(summary["secret_books"], [])

    async def test_tombstoned_album_image_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_root = self._write_pages(root, "dead", 1)
            data = {
                "bookshelf_items": [
                    {
                        "type": "jm_album",
                        "album_id": "dead",
                        "pages": [{"index": 1, "path": str(album_root / "0001.jpg")}],
                    }
                ],
                "jm_cosmos_integration": {"deleted_album_ids": ["dead"]},
            }
            harness = _BookshelfHarness(root, data)
            api = PrivateCompanionPageApi(harness)
            app = Quart(__name__)

            async with app.test_request_context("/bookshelf/image?album_id=dead&page=1"):
                resolved = await api._resolve_bookshelf_image_path_from_request()

            self.assertIsInstance(resolved, dict)
            self.assertEqual(resolved["error"], "图片不存在")

    async def test_mutation_apis_do_not_replace_invalid_legacy_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = {"legacy": "unexpected"}
            data = {"bookshelf_items": original, "jm_cosmos_integration": {}}
            harness = _BookshelfHarness(Path(directory), data)
            api = PrivateCompanionPageApi(harness)
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)
            cases = (
                ("/bookshelf/reading_state", api.update_bookshelf_reading_state, {"album_id": "9009", "page": 1}),
                ("/bookshelf/rate", api.rate_bookshelf_item, {"album_id": "9009", "rating": 8}),
                ("/bookshelf/tags", api.update_bookshelf_item_tags, {"album_id": "9009", "liked_tags": ["测试"]}),
                ("/bookshelf/delete", api.delete_bookshelf_item, {"kind": "jm_album", "album_id": "9009"}),
            )

            for path, handler, payload in cases:
                async with app.test_request_context(
                    path,
                    method="POST",
                    json={**payload, "access_token": token},
                ):
                    result = await handler()
                self.assertFalse(result["success"])
                self.assertIn("结构异常", result["error"])

            self.assertIs(data["bookshelf_items"], original)
            self.assertEqual(harness.save_calls, 0)

    async def test_new_album_write_does_not_replace_invalid_legacy_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = {"legacy": "unexpected"}
            data = {"bookshelf_items": original, "jm_cosmos_integration": {}}
            harness = _BookshelfHarness(Path(directory), data)

            harness._remember_bookshelf_jm_album({"id": "new", "title": "新书"})

            self.assertIs(data["bookshelf_items"], original)
            self.assertEqual(data["bookshelf_items"], {"legacy": "unexpected"})

    async def test_delete_by_album_id_does_not_remove_same_title_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = {
                "bookshelf_items": [
                    {"type": "jm_album", "album_id": "7101", "title": "同名", "pages": []},
                    {"type": "jm_album", "album_id": "7102", "title": "同名", "pages": []},
                ],
                "jm_cosmos_integration": {},
                "bookshelf_secret": {},
                "creative_projects": [],
                "bot_diaries": [],
                "memo_notes": [],
            }
            harness = _BookshelfHarness(Path(directory), data)
            api = PrivateCompanionPageApi(harness)
            token = api._issue_bookshelf_access_token()
            app = Quart(__name__)

            async with app.test_request_context(
                "/bookshelf/delete",
                method="POST",
                json={
                    "kind": "jm_album",
                    "id": "jm-7101",
                    "album_id": "7101",
                    "title": "同名",
                    "access_token": token,
                },
            ):
                result = await api.delete_bookshelf_item()

            self.assertTrue(result["success"])
            self.assertEqual([item["album_id"] for item in data["bookshelf_items"]], ["7102"])
            self.assertEqual(data["jm_cosmos_integration"]["deleted_album_ids"], ["7101"])


if __name__ == "__main__":
    unittest.main()

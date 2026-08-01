# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BookshelfAccessUiTests(unittest.TestCase):
    def test_both_panel_scripts_persist_and_restore_daily_access(self) -> None:
        scripts = [
            ROOT / "pages" / "companion-panel" / "app.js",
            ROOT / "pages" / "陪伴面板" / "app.js",
        ]
        for path in scripts:
            script = path.read_text(encoding="utf-8")
            self.assertIn('const BOOKSHELF_ACCESS_STORAGE_KEY = "pc_bookshelf_access_v1";', script)
            self.assertIn("function persistBookshelfAccess(bookshelf = {})", script)
            self.assertIn("async function restoreBookshelfAccess()", script)
            self.assertIn('postJson("/bookshelf/session", { access_token: stored.token })', script)
            self.assertIn("localStorage.setItem(BOOKSHELF_ACCESS_STORAGE_KEY", script)
            self.assertIn("void restoreBookshelfAccess();", script)


if __name__ == "__main__":
    unittest.main()

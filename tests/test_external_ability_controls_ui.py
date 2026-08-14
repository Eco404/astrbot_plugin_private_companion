# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExternalAbilityControlsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        panel = ROOT / "pages" / "companion-panel"
        localized = ROOT / "pages" / "陪伴面板"
        cls.script = (panel / "app.js").read_text(encoding="utf-8")
        cls.css = (panel / "app.css").read_text(encoding="utf-8")
        cls.html = (panel / "index.html").read_text(encoding="utf-8")
        cls.localized_script = (localized / "app.js").read_text(encoding="utf-8")
        cls.localized_css = (localized / "app.css").read_text(encoding="utf-8")
        cls.localized_html = (localized / "index.html").read_text(encoding="utf-8")

    def test_only_explicit_supported_schema_types_become_controls(self) -> None:
        self.assertIn('new Set(["select", "text", "bool", "number"])', self.script)
        self.assertIn("EXTERNAL_ABILITY_CONTROL_TYPES.has", self.script)
        self.assertIn('name="config" rows="5"', self.script)

    def test_control_save_preserves_unrendered_configuration(self) -> None:
        self.assertIn('name="config" type="hidden"', self.script)
        self.assertIn("const config = { ...(currentConfig || {}) };", self.script)
        self.assertIn("collectExternalAbilityConfig(form, config)", self.script)
        self.assertIn('type === "select"', self.script)
        self.assertIn("JSON.parse(el.value)", self.script)

    def test_controls_have_layout_and_cache_busting(self) -> None:
        self.assertIn(".external-ability-config-fields", self.css)
        self.assertIn(".external-ability-config-field", self.css)
        self.assertRegex(self.html, r'\./app\.css\?v=[^" ]+')
        self.assertRegex(self.html, r'\./app\.js\?v=[^" ]+')

    def test_localized_and_fallback_panels_stay_identical(self) -> None:
        self.assertEqual(self.script, self.localized_script)
        self.assertEqual(self.css, self.localized_css)
        self.assertEqual(self.html, self.localized_html)

    def test_content_extension_owns_legacy_qzone_and_bookshelf_entrypoints(self) -> None:
        for source in (self.script, self.localized_script):
            self.assertIn("if (qzoneTab) qzoneTab.hidden = linked;", source)
            self.assertIn("if (bookshelfTab) bookshelfTab.hidden = linked;", source)
            self.assertIn(
                "enable_qzone_integration: () => !contentCompanionLinked()",
                source,
            )
            self.assertIn("if (linked && [\"qzone\", \"bookshelf\"]", source)


if __name__ == "__main__":
    unittest.main()

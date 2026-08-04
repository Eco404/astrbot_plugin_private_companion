# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL_PATHS = (
    ROOT / "pages" / "companion-panel" / "app.js",
    ROOT / "pages" / "陪伴面板" / "app.js",
)


def _panel_scripts() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in PANEL_PATHS]


def test_multi_persona_panel_scripts_remain_exact_mirrors():
    assert PANEL_PATHS[0].read_bytes() == PANEL_PATHS[1].read_bytes()


def test_persona_display_label_preserves_label_and_full_id():
    for script in _panel_scripts():
        helper_start = script.index("function personaDisplayLabel(")
        helper_end = script.index("function cleanInterjectionText(", helper_start)
        helper = script[helper_start:helper_end]

        assert 'const rawId = String(input?.id ?? personaOrId ?? "");' in helper
        assert (
            'const label = String(persona.label || persona.name || "").trim();'
            in helper
        )
        assert 'display = `${label} · ${id}`;' in helper


def test_multi_persona_selectors_keep_raw_id_as_value_and_use_shared_label():
    for script in _panel_scripts():
        # Page selector, migration selectors, primary selector, and persona checkboxes
        # all submit the untouched ID while delegating visible text to one helper.
        assert script.count('value="${escapeHtml(String(item.id ?? ""))}"') >= 4
        assert script.count("personaDisplayLabel(item)") >= 4
        assert "personaDisplayLabel(id)" in script
        assert "personaDisplayLabel(item, { includeSource: true })" in script

        # Keep the previous label-only rendering from returning in persona controls.
        assert "escapeHtml(item.label || item.id)" not in script
        assert "escapeHtml(item.label || item.name || item.id)" not in script

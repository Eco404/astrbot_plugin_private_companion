from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE_ROOT = ROOT / "pages" / "companion-panel"


def _text(relative: str) -> str:
    return (PAGE_ROOT / relative).read_text(encoding="utf-8")


def test_heavy_panel_scripts_are_lazy_classic_scripts() -> None:
    html = _text("index.html")
    script = _text("app.js")

    assert '<script src="./js/panels/provider-tree.js' not in html
    assert '<script src="./js/panels/qzone-panel.js' not in html
    assert 'loadOptionalClassicScript("./js/panels/provider-tree.js?' in script
    assert 'loadOptionalClassicScript("./js/panels/qzone-panel.js?' in script
    assert "import(\"./js/panels/qzone-panel.js?" not in script
    assert 'providerTree: "PrivateCompanionProviderTree"' in script
    assert 'qzonePanel: "PrivateCompanionQzonePanel"' in script


def test_page_waits_for_bridge_and_keeps_debug_http_fallback() -> None:
    script = _text("app.js")

    assert "async function getReadyPageBridge" in script
    assert "bridge.ready()" in script
    assert "if (isDebugHttpMode()) return null;" in script
    assert "const bridge = await getReadyPageBridge();" in script
    assert "void bootstrapPage();" in script


def test_get_requests_are_deduplicated_only_while_in_flight() -> None:
    script = _text("app.js")

    assert "const inFlightGetRequests = new Map();" in script
    assert 'method === "GET" && dedupe' in script
    assert "inFlightGetRequests.has(requestKey)" in script
    assert "inFlightGetRequests.delete(requestKey)" in script
    assert "const scoped = scopePagePersonaRequest" in script


def test_dashboard_defers_large_user_and_group_lists() -> None:
    script = _text("app.js")

    assert "function scheduleUserGroupPrefetch" in script
    assert "scheduleUserGroupPrefetch(() =>" in script
    assert 'if (tabName !== "dashboard")' in script
    assert "cancelUserGroupPrefetch();" in script


def test_cancelled_view_transitions_do_not_leak_page_errors() -> None:
    script = _text("app.js")

    assert "function watchTabTransition(transition)" in script
    assert "transition.ready?.catch(() => {})" in script
    assert "transition.updateCallbackDone?.catch(() => {})" in script
    assert "transition.finished.then(cleanup, cleanup)" in script
    assert "transition.finished.finally" not in script


def test_responsive_tail_contains_narrow_screen_safety_rules() -> None:
    css = _text("css/polish.css")
    marker = "/* Responsive containment and sticky-stack safety. Keep this layer last. */"
    tail = css.split(marker, 1)[1]

    assert "overflow-x: clip;" in tail
    assert "min-height: 100dvh;" in tail
    assert "min-width: 0;" in tail
    assert "env(safe-area-inset-left)" in tail
    assert "@media (max-width: 900px)" in tail
    assert "@media (max-width: 760px)" in tail
    assert "@media (max-width: 480px)" in tail
    assert ".image-cache-layout," in tail
    assert ".bookcase-layout" in tail
    assert "./css/polish.css?v=20260810-responsive-containment-v1" in _text("index.html")
    assert ".exp-card-toggle input," in tail
    assert ".feature-switch-item input" in tail
    assert "width: 1px;" in tail


def test_ascii_and_utf8_page_mirrors_match_after_optimization() -> None:
    ascii_root = ROOT / "pages" / "companion-panel"
    utf8_root = ROOT / "pages" / "陪伴面板"
    for relative in ("index.html", "app.js", "css/polish.css"):
        assert (ascii_root / relative).read_bytes() == (utf8_root / relative).read_bytes()

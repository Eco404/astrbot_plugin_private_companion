from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL_ROOTS = [ROOT / "pages" / "companion-panel", ROOT / "pages" / "陪伴面板"]


def test_creative_and_reality_are_conditional_companion_workspaces() -> None:
    for panel_root in PANEL_ROOTS:
        html = (panel_root / "index.html").read_text(encoding="utf-8")
        script = (panel_root / "app.js").read_text(encoding="utf-8")
        css = (panel_root / "app.css").read_text(encoding="utf-8")

        assert 'data-tab="creative"' in html
        assert 'data-tab="bookshelf"' not in html
        assert 'data-tab="qzone"' not in html
        assert 'id="panel-creative"' in html
        assert 'id="panel-reality"' in html
        assert html.index('data-tab="experimental"') < html.index('data-tab="reality"')

        assert 'creativeTab.hidden = !creativeInstalled' in script
        assert 'realityTab.hidden = !realityInstalled' in script
        assert 'qzone.classList.remove("panel")' in script
        assert 'creative.appendChild(qzone)' in script
        assert ".annotations .tab[hidden]" in css
        assert ".layout > .panel[hidden]" in css


def test_reality_workspace_exposes_mobile_gateway_without_owning_implementation() -> None:
    script = (PANEL_ROOTS[0] / "app.js").read_text(encoding="utf-8")

    assert 'data-reality-mobile-config' in script
    assert 'action: "save_global_config"' in script
    assert 'postJson("/reality-touch/update"' in script
    assert "function renderRealityTouchPage()" in script
    assert '"enable_experimental_bluetooth_wakeup",\n  "enable_daily_case_review_experiment"' not in script

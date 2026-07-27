from __future__ import annotations

import re
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
APP_JS = (PLUGIN_ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (PLUGIN_ROOT / "pages" / "陪伴面板" / "index.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(
        rf"function {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)(?=\nfunction |\Z)",
        APP_JS,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing JavaScript function: {name}")
    return match.group("body")


class BodyMonitorWebUiPlacementTests(unittest.TestCase):
    def test_health_event_link_is_not_mounted_on_proactive_page(self) -> None:
        self.assertNotIn('id="bodyMonitorIntegrationCard"', INDEX_HTML)
        self.assertNotIn(
            "renderBodyMonitorIntegration();",
            _function_source("renderProactiveCandidates"),
        )

    def test_health_event_link_is_mounted_in_long_term_feature_group(self) -> None:
        source = _function_source("renderFeatureSwitches")

        self.assertIn('group.title === "长线主动"', source)
        self.assertIn('id="bodyMonitorIntegrationCard"', source)
        self.assertIn("renderBodyMonitorIntegration();", source)

    def test_toggle_refreshes_config_feature_branch(self) -> None:
        source = _function_source("renderBodyMonitorIntegration")

        self.assertIn("renderFeatureSwitches();", source)
        self.assertNotIn("renderProactiveCandidates();", source)


if __name__ == "__main__":
    unittest.main()

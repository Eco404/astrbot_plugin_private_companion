# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone

from astrbot_plugin_private_companion.scene_context import SceneContextMixin


class MobileSceneHarness(SceneContextMixin):
    def __init__(self) -> None:
        self.data = {
            "daily_state": {"date": "2026-08-12", "energy": 70, "mood_bias": "平稳"},
            "daily_plan": {"date": "2026-08-12", "items": []},
        }
        self.enable_weather_context = False
        self.enable_weather_alerts = False

    @staticmethod
    def _scene_context_now() -> datetime:
        return datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc)

    @staticmethod
    def _reality_mobile_context(user_id: str):
        return {
            "available": user_id == "owner-1",
            "location": {
                "available": user_id == "owner-1",
                "latitude": 31.23,
                "longitude": 121.474,
                "accuracy_m": 24.0,
                "label": "外出中",
            },
        }


def test_authorized_mobile_location_is_prompt_context_not_primary_location() -> None:
    harness = MobileSceneHarness()
    snapshot = harness._build_companion_scene_snapshot({"user_id": "owner-1"})
    assert snapshot["location"]["text"] == ""
    assert snapshot["location"]["mobile"]["available"] is True
    rendered = harness._format_companion_scene_snapshot(snapshot)
    assert "手机定位上下文" in rendered
    assert "不向用户主动暴露精确坐标" in rendered

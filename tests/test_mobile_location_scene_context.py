# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone

from astrbot_plugin_private_companion.place_cognitive_map import PlaceCognitiveMapMixin
from astrbot_plugin_private_companion.scene_context import SceneContextMixin


class MobileSceneHarness(PlaceCognitiveMapMixin, SceneContextMixin):
    def __init__(self) -> None:
        self.data = {
            "daily_state": {"date": "2026-08-12", "energy": 70, "mood_bias": "平稳"},
            "daily_plan": {"date": "2026-08-12", "items": []},
        }
        self.enable_weather_context = False
        self.enable_weather_alerts = False
        self.events: list[dict] = []

    def _schedule_data_save(self, **_kwargs) -> None:
        return None

    def _place_cognitive_map_emit_memory_event(self, event: dict) -> None:
        self.events.append(event)

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
                "place": {
                    "matched": True,
                    "name": "公司",
                    "kind": "work",
                    "distance_m": 18,
                    "radius_m": 150,
                },
            },
        }


def test_authorized_mobile_location_is_prompt_context_not_primary_location() -> None:
    harness = MobileSceneHarness()
    snapshot = harness._build_companion_scene_snapshot({"user_id": "owner-1"})
    assert snapshot["location"]["text"] == ""
    assert snapshot["location"]["mobile"]["available"] is True
    rendered = harness._format_companion_scene_snapshot(snapshot)
    assert "手机定位上下文" in rendered
    assert "地点档案：公司（工作地点），已在标记地点范围内" in rendered
    assert "地点认知地图" in rendered
    assert "已确认地点：公司（工作地点）" in rendered
    assert "不向用户主动暴露精确坐标" in rendered


def test_authorized_mobile_location_has_dedicated_private_dialogue_context() -> None:
    harness = MobileSceneHarness()

    rendered = harness._format_mobile_user_location_context({"user_id": "owner-1"})

    assert "【用户手机位置感知】" in rendered
    assert "用户当前位于已标记地点“公司”（工作地点）范围内" in rendered
    assert "不得把未标记地点猜成具体住址" in rendered

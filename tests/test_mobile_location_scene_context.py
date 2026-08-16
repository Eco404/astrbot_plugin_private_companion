# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time

from datetime import datetime, timezone

from astrbot_plugin_private_companion.place_cognitive_map import PlaceCognitiveMapMixin
from astrbot_plugin_private_companion.proactive import ProactiveMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin
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
            "available": user_id in {"owner-1", "owner-unmatched", "owner-home"},
            "location": {
                "available": user_id in {"owner-1", "owner-unmatched", "owner-home"},
                "latitude": 31.23,
                "longitude": 121.474,
                "accuracy_m": 24.0,
                "label": "外出中",
                "place": {
                    "matched": user_id in {"owner-1", "owner-home"},
                    "name": "公司" if user_id in {"owner-1", "owner-unmatched"} else "家",
                    "kind": "work" if user_id in {"owner-1", "owner-unmatched"} else "home",
                    "distance_m": 18 if user_id in {"owner-1", "owner-home"} else 0,
                    "radius_m": 150 if user_id in {"owner-1", "owner-home"} else 0,
                },
            },
        }


class ProactiveLocationHarness(ProactiveMixin, ProactiveEngineMixin, PlaceCognitiveMapMixin, SceneContextMixin):
    def __init__(self) -> None:
        self.data = {
            "daily_state": {"energy": 70},
            "daily_plan": {"items": []},
            "daily_weather": {"prompt": "刚下雨"},
        }

    _reality_mobile_context = staticmethod(MobileSceneHarness._reality_mobile_context)

    @staticmethod
    def _weather_summary_text(_weather) -> str:
        return "刚下雨"

    @staticmethod
    def _ordinary_weather_topic_available(_user) -> bool:
        return True

    @staticmethod
    def _proactive_current_agenda_item():
        return None

    @staticmethod
    def _current_story_plan_snapshot():
        return {}

    @staticmethod
    def _private_user_role(_user):
        return "primary"


class ProactiveTransitionHarness(ProactiveLocationHarness):
    def __init__(self) -> None:
        super().__init__()
        self.current_place = ("公司", "work")
        self.data["users"] = {"owner-route": {"user_id": "owner-route", "enabled": True}}
        self.kick_count = 0

    def _reality_mobile_context(self, _user_id: str):
        name, kind = self.current_place
        return {
            "available": True,
            "location": {
                "available": True,
                "latitude": 31.23,
                "longitude": 121.474,
                "place": {
                    "matched": True,
                    "name": name,
                    "kind": kind,
                    "distance_m": 18,
                    "radius_m": 150,
                },
            },
        }

    @staticmethod
    def _reality_companion_api():
        return object()

    @staticmethod
    def _relationship_owner_user_ids():
        return {"owner-route"}

    @staticmethod
    def _configured_target_ids():
        return set()

    @staticmethod
    def _schedule_data_save(**_kwargs):
        return None

    async def _kick_proactive_loop_once(self):
        self.kick_count += 1


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


def test_proactive_mobile_location_is_coarse_and_does_not_expose_coordinates() -> None:
    harness = MobileSceneHarness()

    matched = harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-1"})
    unmatched = harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-unmatched"})

    assert "主动场景位置线索" in matched
    assert "用户当前位于已标记地点“公司”（工作地点）范围内" in matched
    assert "纬度" not in matched and "经度" not in matched
    assert "未命中已标记地点" in unmatched
    assert "31.23" not in unmatched and "121.474" not in unmatched
    assert "不要把位置本身硬写成主动话题" in unmatched


class _CountingBridgeHarness(MobileSceneHarness):
    """Count reality-bridge round trips so the formatter cannot silently regress."""

    def __init__(self) -> None:
        super().__init__()
        self.mobile_context_calls = 0
        self.map_observer_calls = 0

    def _reality_mobile_context(self, user_id: str):
        self.mobile_context_calls += 1
        return MobileSceneHarness._reality_mobile_context(user_id)

    def _observe_mobile_place_context(self, user_id, mobile_location, **kwargs):
        self.map_observer_calls += 1
        return super()._observe_mobile_place_context(user_id, mobile_location, **kwargs)


def test_proactive_location_formatter_queries_reality_bridge_once_per_pass() -> None:
    matched_harness = _CountingBridgeHarness()
    unmatched_harness = _CountingBridgeHarness()

    matched = matched_harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-1"})
    unmatched = unmatched_harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-unmatched"})

    assert "主动场景位置线索" in matched and "主动场景位置线索" in unmatched
    assert matched_harness.mobile_context_calls == 1
    assert matched_harness.map_observer_calls == 1
    assert unmatched_harness.mobile_context_calls == 1
    assert unmatched_harness.map_observer_calls == 1


def test_proactive_weather_timing_uses_workplace_as_context() -> None:
    harness = ProactiveLocationHarness()
    user = {"user_id": "owner-1"}

    topic = harness._choose_proactive_topic("environment_change", user)
    motive = harness._choose_proactive_motive("environment_change", user)

    assert topic == "下班前的这场雨"
    assert "下班回家前" in motive


def test_recent_arrival_at_home_becomes_a_natural_proactive_anchor() -> None:
    harness = ProactiveTransitionHarness()
    user = {"user_id": "owner-route"}

    # The first observation establishes the previous confirmed place; the second
    # one is the actual company -> home transition that can anchor a greeting.
    harness._observe_mobile_place_context(user["user_id"], harness._reality_mobile_context(user["user_id"])["location"], observed_at=100)
    harness.current_place = ("家", "home")

    topic = harness._choose_proactive_topic("check_in", user)
    motive = harness._choose_proactive_motive("check_in", user)

    assert topic == "刚到家后的这一小段"
    assert "刚到家" in motive

    event = harness._pick_mobile_location_arrival_event(user)
    assert event is not None
    assert event["reason"] == "check_in"
    assert event["topic"] == "刚到家后的这一小段"

    user["last_mobile_location_arrival_key"] = event["_mobile_location_transition_key"]
    assert harness._pick_mobile_location_arrival_event(user) is None


def test_mobile_location_watch_wakes_once_for_a_new_transition() -> None:
    harness = ProactiveTransitionHarness()
    user = {"user_id": "owner-route"}
    harness._observe_mobile_place_context(user["user_id"], harness._reality_mobile_context(user["user_id"])["location"], observed_at=100)
    harness.current_place = ("家", "home")

    # The first poll establishes the baseline and deliberately does not send.
    baseline_now = time.time()
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now)) is False
    assert harness.kick_count == 0

    harness.current_place = ("公司", "work")
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now + 60)) is False
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now + 75)) is True
    assert harness.kick_count == 1
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now + 90)) is False
    assert harness.kick_count == 1

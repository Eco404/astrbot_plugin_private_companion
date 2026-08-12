# -*- coding: utf-8 -*-
from __future__ import annotations

from astrbot_plugin_private_companion.place_cognitive_map import PlaceCognitiveMapMixin


class _MapHarness(PlaceCognitiveMapMixin):
    def __init__(self) -> None:
        self.data: dict = {}
        self.saved = 0
        self.events: list[dict] = []

    def _schedule_data_save(self, **_kwargs) -> None:
        self.saved += 1

    def _place_cognitive_map_emit_memory_event(self, event: dict) -> None:
        self.events.append(event)


def _location(name: str, kind: str = "custom", *, matched: bool = True) -> dict:
    return {
        "available": True,
        "place": {
            "matched": matched,
            "name": name,
            "kind": kind,
            "radius_m": 120,
        },
    }


def test_arrival_is_deduplicated_while_the_user_remains_at_a_confirmed_place() -> None:
    harness = _MapHarness()

    first = harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=100)
    second = harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=120)

    assert first["current_place"]["name"] == "公司"
    assert second["known_places"] == [{"name": "公司", "kind": "work"}]
    assert [item["kind"] for item in harness.events] == ["confirmed_place_arrival"]
    assert harness.saved == 1


def test_leaving_and_arriving_elsewhere_creates_a_semantic_route_and_memory_events() -> None:
    harness = _MapHarness()

    harness._observe_mobile_place_context("u1", _location("家", "home"), observed_at=100)
    result = harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=200)

    assert result["current_place"] == {"name": "公司", "kind": "work"}
    assert result["recent_routes"] == [{"from_name": "家", "to_name": "公司", "count": 1}]
    assert [item["kind"] for item in harness.events] == [
        "confirmed_place_arrival",
        "confirmed_place_departure",
        "confirmed_place_arrival",
    ]
    assert "从家前往" in harness.events[-1]["title"]


def test_unmatched_place_does_not_create_any_durable_place_knowledge() -> None:
    harness = _MapHarness()

    result = harness._observe_mobile_place_context("u1", _location("临时猜测", matched=False), observed_at=100)

    assert result["available"] is False
    assert "place_cognitive_maps" not in harness.data or harness.data["place_cognitive_maps"] == {}
    assert harness.events == []

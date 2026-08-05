from __future__ import annotations

import asyncio
import json
import time

import pytest

from astrbot_plugin_private_companion.game_integration import GameIntegrationMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin
from astrbot_plugin_private_companion.scene_context import SceneContextMixin


class GameHarness(GameIntegrationMixin):
    def __init__(self, replies: list[dict]) -> None:
        self.data = {"users": {}}
        self._data_lock = asyncio.Lock()
        self.replies = list(replies)
        self.llm_calls = 0
        self.saved = 0

    def _get_user(self, user_id: str) -> dict:
        return self.data["users"].setdefault(user_id, {"user_id": user_id})

    def _save_data_sync(self) -> None:
        self.saved += 1

    async def _resolve_proactive_persona_prompt(self, _user, *, umo="") -> str:
        return "性格好胜，但很珍惜和用户一起玩的时间。"

    async def _llm_call(self, _prompt: str, **_kwargs) -> str:
        self.llm_calls += 1
        return json.dumps(self.replies.pop(0), ensure_ascii=False)


def round_event(event_id: str, result: str = "bot_loss") -> dict:
    return {
        "event_id": event_id,
        "event_type": "round_finished",
        "user_id": "10001",
        "game": "gomoku",
        "game_label": "五子棋",
        "bot_result": result,
        "room_id": "room-1",
        "round_number": int(event_id.rsplit("-", 1)[-1]),
    }


@pytest.mark.asyncio
async def test_consecutive_losses_stack_with_persona_specific_caps_and_deduplicate() -> None:
    host = GameHarness(
        [
            {
                "competition_delta": -30,
                "companionship_delta": 18,
                "competition_cap": 25,
                "companionship_cap": 30,
                "duration_minutes": 300,
                "rematch_effect": "keep",
                "tone": "嘴上不服，心里玩得很开心",
                "reflection": "输赢和陪伴形成两条不同余味。",
                "invite_interest": 80,
            },
            {
                "competition_delta": -30,
                "companionship_delta": 18,
                "competition_cap": 35,
                "companionship_cap": 25,
                "duration_minutes": 600,
                "rematch_effect": "keep",
                "tone": "连续输了，更想认真赢回来",
                "reflection": "连败叠加到人格允许的上限。",
                "invite_interest": 95,
            },
        ]
    )

    first = await host._record_external_game_event(round_event("event-1"))
    second = await host._record_external_game_event(round_event("event-2"))
    duplicate = await host._record_external_game_event(round_event("event-2"))

    state = host.data["users"]["10001"]["game_afterglow"]
    assert first["ok"] and second["ok"]
    assert duplicate["duplicate"] is True
    assert host.llm_calls == 2
    assert state["competition_charge"] == -35
    assert state["companionship_warmth"] == 25
    assert state["streak_result"] == "bot_loss"
    assert state["streak_count"] == 2
    assert state["stats"]["rounds"] == 2
    assert state["stats"]["bot_losses"] == 2


@pytest.mark.asyncio
async def test_rematch_tone_can_clear_existing_afterglow() -> None:
    host = GameHarness(
        [
            {
                "competition_delta": -12,
                "companionship_delta": 8,
                "competition_cap": 30,
                "companionship_cap": 40,
                "duration_minutes": 180,
                "rematch_effect": "keep",
                "tone": "有点不服",
                "reflection": "还想着上一局。",
                "invite_interest": 70,
            },
            {
                "competition_delta": 0,
                "companionship_delta": 0,
                "competition_cap": 30,
                "companionship_cap": 40,
                "duration_minutes": 0,
                "rematch_effect": "clear",
                "tone": "翻篇重新玩",
                "reflection": "用户的语气让上一局自然翻篇。",
                "invite_interest": 85,
            },
        ]
    )
    await host._record_external_game_event(round_event("event-1"))
    result = await host._record_external_game_event(
        {
            "event_id": "rematch-1",
            "event_type": "rematch_requested",
            "user_id": "10001",
            "game": "gomoku",
            "game_label": "五子棋",
            "request_text": "刚才算我走神，我们重新认真来一局吧",
        }
    )

    state = host.data["users"]["10001"]["game_afterglow"]
    assert result["afterglow"]["active"] is False
    assert state["competition_charge"] == 0
    assert state["companionship_warmth"] == 0


def test_afterglow_prompt_hides_expired_state() -> None:
    host = GameHarness([])
    user = {
        "game_afterglow": {
            "game_label": "五子棋",
            "tone": "还在惦记输掉的那一局",
            "reflection": "想找机会赢回来。",
            "competition_charge": -20,
            "expires_at": time.time() + 60,
        }
    }
    assert "五子棋" in host._format_game_afterglow_prompt(user)
    user["game_afterglow"]["expires_at"] = time.time() - 1
    assert host._format_game_afterglow_prompt(user) == ""


@pytest.mark.asyncio
async def test_expired_charge_does_not_stack_into_a_new_afterglow() -> None:
    host = GameHarness(
        [
            {
                "competition_delta": -5,
                "companionship_delta": 4,
                "competition_cap": 100,
                "companionship_cap": 100,
                "duration_minutes": 60,
                "rematch_effect": "keep",
                "tone": "又有一点不服",
                "reflection": "这是新的余韵。",
                "invite_interest": 50,
            }
        ]
    )
    host.data["users"]["10001"] = {
        "user_id": "10001",
        "game_afterglow": {
            "competition_charge": -80,
            "companionship_warmth": 70,
            "expires_at": time.time() - 10,
            "recent_event_ids": [],
        },
    }

    await host._record_external_game_event(round_event("event-1"))

    state = host.data["users"]["10001"]["game_afterglow"]
    assert state["competition_charge"] == -5
    assert state["companionship_warmth"] == 4


def test_scene_context_formats_active_game_afterglow_as_tone_only() -> None:
    host = SceneContextMixin()
    rendered = host._format_companion_scene_snapshot(
        {
            "date": "2026-08-05",
            "time": "20:30",
            "daypart": "晚上",
            "state": {"energy_label": "平稳", "mood": "平稳"},
            "game_afterglow": {
                "active": True,
                "game_label": "五子棋",
                "tone": "嘴上还有点不服",
                "reflection": "但很享受一起玩的时间。",
            },
        }
    )

    assert "五子棋" in rendered
    assert "嘴上还有点不服" in rendered
    assert "competition_charge" not in rendered


def test_external_ability_cooldown_and_availability_are_per_user() -> None:
    host = ProactiveEngineMixin()
    host._external_proactive_abilities = {
        "game": {"executor": lambda _ctx: None, "availability": lambda ctx: ctx["user"].get("allowed", False)}
    }
    item = {
        "name": "game",
        "enabled": True,
        "available": True,
        "min_interval_hours": 24,
        "last_executed_ts": time.time(),
    }
    host.external_proactive_abilities = lambda: [item]
    host._external_ability_config = lambda _name: {}

    recent_user = {
        "allowed": True,
        "external_proactive_ability_last": {"game": time.time()},
    }
    other_user = {"allowed": True}
    blocked_user = {"allowed": False}

    assert host._available_external_proactive_abilities(recent_user) == []
    assert host._available_external_proactive_abilities(other_user) == [item]
    assert host._available_external_proactive_abilities(blocked_user) == []

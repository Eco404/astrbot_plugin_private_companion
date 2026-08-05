from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from copy import deepcopy
from typing import Any

from astrbot.api import logger

from .helpers import _safe_float, _safe_int, _single_line


GAME_EVENT_TYPES = frozenset({"round_finished", "rematch_requested"})
GAME_RESULTS = frozenset({"bot_win", "bot_loss", "draw", "completed"})
REMATCH_EFFECTS = frozenset({"clear", "shorten", "keep", "extend"})


class GameIntegrationMixin:
    """Optional game events and persona-shaped emotional afterglow."""

    @staticmethod
    def _game_json_object(raw: Any) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        for candidate in re.findall(r"\{.*?\}", text, re.DOTALL):
            try:
                value = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _normalize_external_game_event(payload: Any) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        event_type = _single_line(source.get("event_type"), 40).lower()
        user_id = _single_line(source.get("user_id"), 80)
        game = _single_line(source.get("game"), 40).lower()
        result = _single_line(source.get("bot_result"), 24).lower()
        if event_type not in GAME_EVENT_TYPES or not user_id:
            return {}
        if event_type == "round_finished" and result not in GAME_RESULTS:
            return {}
        if event_type == "rematch_requested" and result not in GAME_RESULTS:
            result = "completed"
        normalized = {
            "event_type": event_type,
            "event_id": _single_line(source.get("event_id"), 160),
            "user_id": user_id,
            "user_name": _single_line(source.get("user_name"), 80),
            "game": game or "unknown",
            "game_label": _single_line(source.get("game_label"), 40) or game or "游戏",
            "bot_result": result,
            "request_text": _single_line(source.get("request_text"), 240),
            "recent_context": _single_line(source.get("recent_context"), 900),
            "room_id": _single_line(source.get("room_id"), 100),
            "session_id": _single_line(source.get("session_id"), 200),
            "scope": _single_line(source.get("scope"), 20),
            "difficulty": _single_line(source.get("difficulty"), 24),
            "round_number": _safe_int(source.get("round_number"), 0, 0, 100000),
            "score": deepcopy(source.get("score")) if isinstance(source.get("score"), dict) else {},
            "occurred_at": _safe_float(source.get("occurred_at"), time.time(), 0.0),
            "source_plugin": _single_line(source.get("source_plugin"), 100) or "external",
        }
        if not normalized["event_id"]:
            identity = json.dumps(
                {
                    key: normalized[key]
                    for key in (
                        "event_type",
                        "user_id",
                        "game",
                        "bot_result",
                        "room_id",
                        "round_number",
                        "occurred_at",
                    )
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            normalized["event_id"] = "game:" + hashlib.sha1(identity.encode("utf-8")).hexdigest()
        return normalized

    @staticmethod
    def _game_afterglow_streak(previous: dict[str, Any], event: dict[str, Any]) -> tuple[str, int]:
        result = _single_line(event.get("bot_result"), 24)
        if result not in {"bot_win", "bot_loss"}:
            return "", 0
        if _single_line(previous.get("streak_result"), 24) == result:
            return result, min(999, _safe_int(previous.get("streak_count"), 0, 0) + 1)
        return result, 1

    @staticmethod
    def _fallback_game_afterglow_assessment(
        event: dict[str, Any],
        previous: dict[str, Any],
        *,
        streak_count: int,
    ) -> dict[str, Any]:
        event_type = event["event_type"]
        result = event.get("bot_result")
        if event_type == "rematch_requested":
            return {
                "competition_delta": 0,
                "companionship_delta": 3,
                "competition_cap": _safe_int(previous.get("competition_cap"), 30, 0, 100),
                "companionship_cap": _safe_int(previous.get("companionship_cap"), 50, 0, 100),
                "duration_minutes": 180,
                "rematch_effect": "extend",
                "tone": _single_line(previous.get("tone"), 160) or "愿意顺着这股兴致继续玩",
                "reflection": "用户主动提出再来一局，这次互动仍有继续发展的余味。",
                "invite_interest": max(70, _safe_int(previous.get("invite_interest"), 0, 0, 100)),
            }
        multiplier = min(2.5, 1.0 + max(0, streak_count - 1) * 0.25)
        if result == "bot_loss":
            competition_delta = -round(10 * multiplier)
            tone = "有点不服气，但也享受和用户一起玩的过程"
            reflection = "输了会留下短暂的不服气，共同参与本身仍是正向体验。"
        elif result == "bot_win":
            competition_delta = round(6 * multiplier)
            tone = "有一点得意，也愿意继续陪用户玩"
            reflection = "赢下这一局带来一点得意，共同参与仍比胜负更重要。"
        else:
            competition_delta = 0
            tone = "还留着一起玩的轻松兴致"
            reflection = "胜负没有形成明显情绪，共同参与留下了轻松余味。"
        return {
            "competition_delta": competition_delta,
            "companionship_delta": min(18, round(8 * multiplier)),
            "competition_cap": 30,
            "companionship_cap": 50,
            "duration_minutes": 180 if streak_count >= 2 else 120,
            "rematch_effect": "keep",
            "tone": tone,
            "reflection": reflection,
            "invite_interest": min(90, 58 + streak_count * 8),
        }

    async def _assess_external_game_afterglow(
        self,
        event: dict[str, Any],
        previous: dict[str, Any],
        *,
        streak_count: int,
        user_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        persona = ""
        resolver = getattr(self, "_resolve_proactive_persona_prompt", None)
        if callable(resolver):
            try:
                value = resolver(user_snapshot, umo=_single_line(event.get("session_id"), 200))
                persona = str(await value if inspect.isawaitable(value) else value or "")
            except Exception as exc:
                logger.debug("[PrivateCompanion] 游戏余韵读取人格失败: %s", _single_line(exc, 120))
        if not persona:
            getter = getattr(self, "_get_default_persona_prompt", None)
            if callable(getter):
                try:
                    persona = str(getter() or "")
                except Exception:
                    persona = ""
        prompt_payload = {
            "event": event,
            "user_context": {
                "nickname": _single_line(
                    user_snapshot.get("nickname")
                    or user_snapshot.get("display_name"),
                    80,
                ),
                "style": _single_line(user_snapshot.get("style"), 120),
                "relationship_role": _single_line(
                    user_snapshot.get("relationship_role"), 30
                ),
                "relationship_mode": _single_line(
                    user_snapshot.get("relationship_mode"), 40
                ),
                "current_interaction": deepcopy(
                    user_snapshot.get("current_interaction")
                )
                if isinstance(user_snapshot.get("current_interaction"), dict)
                else {},
            },
            "previous_afterglow": {
                key: previous.get(key)
                for key in (
                    "competition_charge",
                    "companionship_warmth",
                    "competition_cap",
                    "companionship_cap",
                    "tone",
                    "reflection",
                    "streak_result",
                    "streak_count",
                )
            },
            "new_streak_count": streak_count,
        }
        prompt = f"""
你负责根据 Bot 人格结算一次游戏互动后的短期情绪余韵，不生成对用户的回复。

【Bot 人格】
{persona[:3200] or "没有明确人格；采用温和、不过度在意胜负的默认倾向。"}

【结构化游戏事件】
{json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)}

判断重点：
- 有的人格非常在乎输赢，有的人格更看重陪用户玩了这件事，两条维度必须分开。
- 连续胜负可以叠加，但 competition_cap 和 companionship_cap 必须按人格给出不同上限。
- round_finished 的 competition_delta：Bot 输且不服可为负，Bot 赢且得意可为正；不在乎可接近 0。
- companionship_delta 表示共同参与留下的温度，可以为 0，但不要仅因输掉正常游戏就把它强行改成负数。
- rematch_requested 要结合 request_text 的上下文和语气决定 clear、shorten、keep 或 extend；不能机械延长。
- recent_context 只包含该用户在当前房间最近几轮自己的对话，用它理解 request_text，不要假设其中出现了其他玩家。
- 余韵只影响之后的语气、主动动机和是否想再玩，不得改写长期关系，也不得当成被用户伤害。
- tone/reflection 是内部提示，不得包含插件、模型、分数、阈值等系统词。

只输出 JSON：
{{"competition_delta":-40到40整数,"companionship_delta":0到40整数,"competition_cap":0到100整数,"companionship_cap":0到100整数,"duration_minutes":0到10080整数,"rematch_effect":"clear|shorten|keep|extend","tone":"一句当前语气底色","reflection":"一句内部余味","invite_interest":0到100整数}}
""".strip()
        fallback = self._fallback_game_afterglow_assessment(
            event, previous, streak_count=streak_count
        )
        caller = getattr(self, "_llm_call", None)
        if not callable(caller):
            return fallback
        try:
            raw = await caller(prompt, max_tokens=260, task="game_emotional_afterglow")
            parsed = self._game_json_object(raw)
        except Exception as exc:
            logger.debug("[PrivateCompanion] 游戏余韵模型判断失败: %s", _single_line(exc, 120))
            return fallback
        if not parsed:
            return fallback
        return {
            "competition_delta": _safe_int(parsed.get("competition_delta"), fallback["competition_delta"], -40, 40),
            "companionship_delta": _safe_int(parsed.get("companionship_delta"), fallback["companionship_delta"], 0, 40),
            "competition_cap": _safe_int(parsed.get("competition_cap"), fallback["competition_cap"], 0, 100),
            "companionship_cap": _safe_int(parsed.get("companionship_cap"), fallback["companionship_cap"], 0, 100),
            "duration_minutes": _safe_int(parsed.get("duration_minutes"), fallback["duration_minutes"], 0, 10080),
            "rematch_effect": (
                _single_line(parsed.get("rematch_effect"), 20).lower()
                if _single_line(parsed.get("rematch_effect"), 20).lower() in REMATCH_EFFECTS
                else fallback["rematch_effect"]
            ),
            "tone": _single_line(parsed.get("tone"), 160) or fallback["tone"],
            "reflection": _single_line(parsed.get("reflection"), 240) or fallback["reflection"],
            "invite_interest": _safe_int(parsed.get("invite_interest"), fallback["invite_interest"], 0, 100),
        }

    @staticmethod
    def _game_afterglow_public_view(state: Any, *, now: float | None = None) -> dict[str, Any]:
        raw = state if isinstance(state, dict) else {}
        current = time.time() if now is None else float(now)
        expires_at = _safe_float(raw.get("expires_at"), 0.0, 0.0)
        active = bool(expires_at > current and (
            _safe_int(raw.get("competition_charge"), 0, -100, 100)
            or _safe_int(raw.get("companionship_warmth"), 0, 0, 100)
            or _single_line(raw.get("tone"), 160)
        ))
        return {
            "active": active,
            "game": _single_line(raw.get("game"), 40),
            "game_label": _single_line(raw.get("game_label"), 40),
            "tone": _single_line(raw.get("tone"), 160) if active else "",
            "reflection": _single_line(raw.get("reflection"), 240) if active else "",
            "streak_result": _single_line(raw.get("streak_result"), 24),
            "streak_count": _safe_int(raw.get("streak_count"), 0, 0, 999),
            "invite_interest": _safe_int(raw.get("invite_interest"), 0, 0, 100),
            "remaining_minutes": max(0, int((expires_at - current + 59) // 60)) if active else 0,
            "last_event_at": _safe_float(raw.get("last_event_at"), 0.0, 0.0),
            "stats": deepcopy(raw.get("stats")) if isinstance(raw.get("stats"), dict) else {},
        }

    def _format_game_afterglow_prompt(self, user: dict[str, Any] | None) -> str:
        raw = user.get("game_afterglow") if isinstance(user, dict) else {}
        view = self._game_afterglow_public_view(raw)
        if not view.get("active"):
            return ""
        game_label = _single_line(view.get("game_label"), 40) or "刚才的游戏"
        tone = _single_line(view.get("tone"), 160)
        reflection = _single_line(view.get("reflection"), 240)
        details = "；".join(part for part in (tone, reflection) if part)
        return _single_line(
            f"游戏情绪余韵：{game_label}留下了{details or '一点尚未散去的余味'}。"
            "它只影响自然语气、是否想再玩和相关话题承接；不要复述内部状态、不要把正常胜负说成关系受伤。",
            520,
        )

    async def _record_external_game_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = self._normalize_external_game_event(payload)
        if not event:
            return {"ok": False, "reason": "invalid_game_event"}
        user_id = event["user_id"]
        async with self._data_lock:
            user = self._get_user(user_id)
            previous = deepcopy(user.get("game_afterglow")) if isinstance(user.get("game_afterglow"), dict) else {}
            recent_ids = [
                _single_line(item, 160)
                for item in (previous.get("recent_event_ids") or [])
                if _single_line(item, 160)
            ]
            if event["event_id"] in recent_ids:
                return {
                    "ok": True,
                    "duplicate": True,
                    "afterglow": self._game_afterglow_public_view(previous),
                }
            user_snapshot = deepcopy(user)
        streak_result, streak_count = self._game_afterglow_streak(previous, event)
        assessment = await self._assess_external_game_afterglow(
            event,
            previous,
            streak_count=streak_count,
            user_snapshot=user_snapshot,
        )
        now = time.time()
        async with self._data_lock:
            user = self._get_user(user_id)
            current = deepcopy(user.get("game_afterglow")) if isinstance(user.get("game_afterglow"), dict) else {}
            recent_ids = [
                _single_line(item, 160)
                for item in (current.get("recent_event_ids") or [])
                if _single_line(item, 160)
            ]
            if event["event_id"] in recent_ids:
                return {
                    "ok": True,
                    "duplicate": True,
                    "afterglow": self._game_afterglow_public_view(current, now=now),
                }
            competition_cap = assessment["competition_cap"]
            companionship_cap = assessment["companionship_cap"]
            previous_expiry = _safe_float(current.get("expires_at"), 0.0, 0.0)
            afterglow_was_active = previous_expiry > now
            base_competition = (
                _safe_int(current.get("competition_charge"), 0, -100, 100)
                if afterglow_was_active
                else 0
            )
            base_companionship = (
                _safe_int(current.get("companionship_warmth"), 0, 0, 100)
                if afterglow_was_active
                else 0
            )
            competition = max(
                -competition_cap,
                min(
                    competition_cap,
                    base_competition + assessment["competition_delta"],
                ),
            )
            companionship = max(
                0,
                min(
                    companionship_cap,
                    base_companionship + assessment["companionship_delta"],
                ),
            )
            duration_seconds = assessment["duration_minutes"] * 60
            if event["event_type"] == "rematch_requested":
                effect = assessment["rematch_effect"]
                if effect == "clear":
                    competition = 0
                    companionship = 0
                    expires_at = now
                elif effect == "shorten":
                    expires_at = min(previous_expiry or now, now + duration_seconds)
                elif effect == "extend":
                    expires_at = max(previous_expiry, now + duration_seconds)
                else:
                    expires_at = (
                        previous_expiry
                        if previous_expiry > now
                        else now + duration_seconds
                    )
            else:
                expires_at = max(previous_expiry, now + duration_seconds)
            stats = deepcopy(current.get("stats")) if isinstance(current.get("stats"), dict) else {}
            if event["event_type"] == "round_finished":
                stats["rounds"] = _safe_int(stats.get("rounds"), 0, 0) + 1
                result_key = {
                    "bot_win": "bot_wins",
                    "bot_loss": "bot_losses",
                    "draw": "draws",
                    "completed": "completed",
                }.get(event["bot_result"], "completed")
                stats[result_key] = _safe_int(stats.get(result_key), 0, 0) + 1
            recent_ids.append(event["event_id"])
            updated = {
                **current,
                "version": 1,
                "game": event["game"],
                "game_label": event["game_label"],
                "competition_charge": competition,
                "companionship_warmth": companionship,
                "competition_cap": competition_cap,
                "companionship_cap": companionship_cap,
                "tone": assessment["tone"],
                "reflection": assessment["reflection"],
                "invite_interest": assessment["invite_interest"],
                "streak_result": streak_result if event["event_type"] == "round_finished" else _single_line(current.get("streak_result"), 24),
                "streak_count": streak_count if event["event_type"] == "round_finished" else _safe_int(current.get("streak_count"), 0, 0, 999),
                "last_result": event["bot_result"],
                "last_event_type": event["event_type"],
                "last_event_at": now,
                "updated_at": now,
                "expires_at": expires_at,
                "stats": stats,
                "recent_event_ids": recent_ids[-32:],
                "last_event": {
                    key: event[key]
                    for key in (
                        "event_type",
                        "game",
                        "game_label",
                        "bot_result",
                        "room_id",
                        "round_number",
                        "request_text",
                    )
                },
            }
            user["game_afterglow"] = updated
            self._save_data_sync()
        logger.info(
            "[PrivateCompanion] 游戏余韵已结算: user=%s game=%s result=%s streak=%s competition=%s companionship=%s",
            user_id,
            event["game"],
            event["bot_result"],
            updated["streak_count"],
            competition,
            companionship,
        )
        return {
            "ok": True,
            "duplicate": False,
            "afterglow": self._game_afterglow_public_view(updated, now=now),
        }

# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from astrbot.api import logger

from .helpers import _safe_float, _safe_int, _single_line


class BusyReplyGateMixin:
    """Delay passive replies and reschedule proactive messages while Bot is busy."""

    _BUSY_ACTIVITY_PATTERN = re.compile(
        r"上课|课堂|听课|自习|学习|复习|预习|写作业|做作业|赶作业|做题|考试|测验|"
        r"开会|会议|工作|办公|值班|实习|训练|排练|实验|赶稿|写稿|编程|写代码|"
        r"处理(?:事情|任务|工作)|专注|集中精神|忙(?:着|碌|工作|学习)|通勤|赶路"
    )
    _BUSY_ACTIVITY_EXCLUSION_PATTERN = re.compile(
        r"睡觉|睡眠|午睡|午休|补觉|休息|发呆|摸鱼|放松|吃饭|用餐|散步|刷视频|"
        r"看番|打游戏|玩游戏|聊天|自由时间|准备睡|洗漱|刚醒|起床"
    )
    _BUSY_URGENT_PATTERN = re.compile(
        r"救命|出事|紧急|急事|报警|医院|受伤|流血|疼得|不舒服|呼吸困难|"
        r"害怕|崩溃|自杀|不想活|马上回|立刻回|快回|现在就回|十万火急"
    )
    _BUSY_PROACTIVE_EXEMPT_SOURCES = {
        "timer",
        "troubleshooting",
        "simulation",
        "memo_note",
        "environment_change",
    }
    _BUSY_PROACTIVE_EXEMPT_REASONS = {
        "timer",
        "memo_note_reminder",
        "environment_change",
    }

    def _busy_reply_presence_status(self, item: dict[str, Any]) -> tuple[str, str]:
        key = _single_line(item.get("key"), 80)
        enhanced = self.data.get("detail_enhanced_segments") if isinstance(getattr(self, "data", None), dict) else None
        detail = enhanced.get(key) if key and isinstance(enhanced, dict) else None
        status = detail.get("presence_status") if isinstance(detail, dict) else None
        if not isinstance(status, dict):
            return "", ""
        mode = _single_line(status.get("mode") or status.get("status"), 24).lower()
        label = _single_line(
            status.get("custom_text")
            or status.get("wording")
            or status.get("text")
            or status.get("label"),
            40,
        )
        return mode, label

    def _busy_reply_item_is_busy(self, item: dict[str, Any] | None) -> tuple[bool, str]:
        if not isinstance(item, dict):
            return False, "no_current_schedule"
        lifecycle_normalizer = getattr(self, "_normalize_schedule_lifecycle_status", None)
        lifecycle = (
            lifecycle_normalizer(item.get("lifecycle_status"))
            if callable(lifecycle_normalizer)
            else _single_line(item.get("lifecycle_status"), 24).lower()
        )
        if lifecycle in {"cancelled", "completed", "skipped"}:
            return False, f"lifecycle:{lifecycle}"
        sleepy_checker = getattr(self, "_is_sleepy_plan_item", None)
        if callable(sleepy_checker):
            try:
                if sleepy_checker(item):
                    return False, "sleep_schedule"
            except Exception:
                pass
        mode, label = self._busy_reply_presence_status(item)
        if mode in {"busy", "忙碌"}:
            return True, "presence:busy"
        if mode in {"custom", "自定义", "自定义状态"} and re.search(r"忙|专注|上课|学习|工作|开会", label):
            return True, f"presence:{label}"
        activity = _single_line(item.get("activity"), 220)
        mood = _single_line(item.get("mood"), 60)
        text = f"{activity} {mood}".strip()
        if not text:
            return False, "empty_schedule"
        if self._BUSY_ACTIVITY_EXCLUSION_PATTERN.search(text):
            return False, "leisure_or_rest_schedule"
        if self._BUSY_ACTIVITY_PATTERN.search(text):
            return True, "schedule_keyword"
        return False, "schedule_not_busy"

    def _busy_reply_item_end_at(self, plan: dict[str, Any], current_item: dict[str, Any]) -> float:
        items = plan.get("items") if isinstance(plan.get("items"), list) else []
        if not items:
            return 0.0
        starts_getter = getattr(self, "_normalized_plan_item_starts", None)
        current_minutes_getter = getattr(self, "_effective_plan_now_minutes", None)
        end_getter = getattr(self, "_plan_item_end_minutes", None)
        if not all(callable(item) for item in (starts_getter, current_minutes_getter, end_getter)):
            return 0.0
        try:
            starts = starts_getter(items)
            current_minutes = current_minutes_getter(str(plan.get("date") or ""))
        except Exception:
            return 0.0
        if current_minutes is None:
            return 0.0
        selected_index = -1
        for index, item in enumerate(items):
            if item is current_item:
                selected_index = index
                break
        if selected_index < 0:
            current_key = _single_line(current_item.get("key") or current_item.get("id"), 80)
            if current_key:
                selected_index = next(
                    (
                        index
                        for index, item in enumerate(items)
                        if isinstance(item, dict)
                        and _single_line(item.get("key") or item.get("id"), 80) == current_key
                    ),
                    -1,
                )
        if selected_index < 0 or selected_index >= len(starts):
            return 0.0
        start_minutes = starts[selected_index]
        if start_minutes is None:
            return 0.0
        next_start = next((value for value in starts[selected_index + 1 :] if value is not None), None)
        try:
            end_minutes = end_getter(start_minutes, current_item, next_start=next_start)
        except Exception:
            return 0.0
        remaining_seconds = max(0, int(end_minutes - current_minutes) * 60)
        if remaining_seconds <= 0:
            return 0.0
        now_getter = getattr(self, "_environment_now", None)
        try:
            now_ts = float(now_getter().timestamp()) if callable(now_getter) else 0.0
        except Exception:
            now_ts = 0.0
        return now_ts + remaining_seconds if now_ts > 0 else 0.0

    def _busy_reply_context(self) -> dict[str, Any]:
        if not bool(getattr(self, "enable_busy_reply_gate", False)):
            return {"busy": False, "reason": "disabled", "until": 0.0, "schedule": ""}
        data = getattr(self, "data", None)
        plan = data.get("daily_plan") if isinstance(data, dict) and isinstance(data.get("daily_plan"), dict) else {}
        current_getter = getattr(self, "_get_current_plan_item", None)
        try:
            current_item = current_getter(plan) if callable(current_getter) else None
        except Exception:
            current_item = None
        busy, reason = self._busy_reply_item_is_busy(current_item)
        formatter = getattr(self, "_format_plan_item_for_prompt", None)
        try:
            schedule = formatter(current_item) if busy and callable(formatter) else ""
        except Exception:
            schedule = ""
        until = self._busy_reply_item_end_at(plan, current_item) if busy and isinstance(current_item, dict) else 0.0
        return {
            "busy": busy,
            "reason": reason,
            "until": until,
            "schedule": _single_line(schedule, 220),
            "item": current_item if isinstance(current_item, dict) else None,
        }

    @classmethod
    def _busy_reply_bypass_reason(cls, text: Any) -> str:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return ""
        if cls._BUSY_URGENT_PATTERN.search(compact):
            return "urgent_or_safety"
        if re.match(r"^[!！/／]", compact):
            return "command"
        if re.search(r"^(?:陪伴|私聊陪伴).{0,8}(?:设置|配置|开启|关闭|启用|停用|检查|修复|诊断|状态)", compact):
            return "management_command"
        return ""

    async def _apply_busy_reply_gate_delay(self, event: Any, *, is_private_chat: bool) -> tuple[float, str]:
        if not bool(getattr(self, "enable_busy_reply_gate", False)):
            return 0.0, "disabled"
        if bool(getattr(event, "_private_companion_busy_reply_delay_applied", False)):
            return 0.0, "already_applied"
        try:
            setattr(event, "_private_companion_busy_reply_delay_applied", True)
        except Exception:
            pass
        context = self._busy_reply_context()
        if not bool(context.get("busy")):
            return 0.0, str(context.get("reason") or "not_busy")
        bypass = self._busy_reply_bypass_reason(getattr(event, "message_str", ""))
        if bypass:
            return 0.0, f"bypass:{bypass}"
        minimum = _safe_int(getattr(self, "busy_reply_min_delay_seconds", 60), 60, 0)
        maximum = _safe_int(getattr(self, "busy_reply_max_delay_seconds", 300), 300, 0)
        minimum = min(900, minimum)
        maximum = min(900, maximum)
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        if not is_private_chat:
            minimum = min(minimum, 12)
            maximum = min(maximum, 12)
        delay = random.uniform(minimum, maximum) if maximum > minimum else float(minimum)
        if delay <= 0:
            return 0.0, "zero_delay"
        try:
            setattr(event, "private_companion_busy_reply_delay_seconds", delay)
            setattr(event, "private_companion_busy_reply_schedule", context.get("schedule") or "")
            setattr(event, "private_companion_busy_reply_until", _safe_float(context.get("until"), 0.0))
        except Exception:
            pass
        logger.info(
            "[PrivateCompanion] 繁忙回复闸门延迟本轮被动回复: session=%s delay=%.1fs private=%s schedule=%s",
            _single_line(getattr(event, "unified_msg_origin", ""), 120) or "unknown",
            delay,
            is_private_chat,
            _single_line(context.get("schedule"), 140) or context.get("reason"),
        )
        await asyncio.sleep(delay)
        return delay, str(context.get("reason") or "busy")

    def _busy_reply_proactive_block_until(
        self,
        user: dict[str, Any] | None,
        *,
        now: float,
        reason: Any = "",
        source: Any = "",
    ) -> float:
        if not bool(getattr(self, "enable_busy_reply_gate", False)):
            return 0.0
        normalized_reason = _single_line(reason, 48).lower()
        normalized_source = _single_line(source, 48).lower()
        if normalized_source in self._BUSY_PROACTIVE_EXEMPT_SOURCES:
            return 0.0
        if normalized_reason in self._BUSY_PROACTIVE_EXEMPT_REASONS:
            return 0.0
        context = self._busy_reply_context()
        if not bool(context.get("busy")):
            return 0.0
        until = _safe_float(context.get("until"), 0.0)
        if until <= now:
            until = now + 15 * 60
        buffer_minutes = _safe_int(
            getattr(self, "busy_reply_proactive_resume_buffer_minutes", 10),
            10,
            0,
        )
        return until + min(120, buffer_minutes) * 60

    def _defer_proactive_for_busy(self, user: dict[str, Any], *, now: float, until: float) -> bool:
        if not isinstance(user, dict) or until <= now:
            return False
        current = _safe_float(user.get("next_proactive_at"), 0.0)
        if current > until:
            return False
        base = current if current > 0 else now
        shift = max(0.0, until - base)
        user["next_proactive_at"] = until
        for key in (
            "planned_proactive_window_start_at",
            "planned_proactive_preferred_at",
            "planned_proactive_best_until_at",
            "planned_proactive_expire_at",
        ):
            value = _safe_float(user.get(key), 0.0)
            if value > 0:
                user[key] = value + shift
        if _safe_float(user.get("planned_proactive_window_start_at"), 0.0) <= 0:
            user["planned_proactive_window_start_at"] = until
            user["planned_proactive_best_until_at"] = until + 45 * 60
            user["planned_proactive_expire_at"] = until + 90 * 60
        user["busy_reply_deferred_until"] = until
        user["busy_reply_deferred_at"] = now
        return True

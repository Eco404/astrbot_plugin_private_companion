# -*- coding: utf-8 -*-
"""现实触及：backed by the host's default audio output."""

from __future__ import annotations

import asyncio
import copy
import re
import zoneinfo
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from .helpers import _now_ts, _safe_int, _single_line
from .reality_touch_audio import RealityTouchAudioMixin


class WakeupAlarmMixin(RealityTouchAudioMixin):
    """Schedule local TTS contact without coupling to a Bluetooth vendor API."""

    _WAKEUP_DEFAULT_MESSAGE = "早上好，该起床啦。先坐起来喝口水，再慢慢开始今天吧。"
    _WAKEUP_DYNAMIC_MESSAGE_HINT = "每次触发时按人格、关系与当天语境动态生成"
    _REALITY_TOUCH_CONSENT_VERSION = 1
    _REALITY_TOUCH_CONFIRMATION_TEXT = (
        "我已知晓现实触及会调用本机音频输出，并同意启用当前音频能力；"
        "未来摄像头能力需要再次单独确认"
    )
    _WAKEUP_CONTACT_ACTIVE_STATES = {"pending", "playing", "snoozed"}
    _WAKEUP_DELIVERY_MODES = {"audio_only", "audio_and_chat", "chat_on_failure"}

    def _reality_touch_consent(self, user: dict[str, Any]) -> dict[str, Any]:
        consent = user.get("reality_touch_consent")
        return consent if isinstance(consent, dict) else {}

    def _reality_touch_capability_consented(self, user: dict[str, Any], capability: str) -> bool:
        consent = self._reality_touch_consent(user)
        capabilities = consent.get("granted_capabilities")
        return (
            consent.get("confirmed") is True
            and _safe_int(consent.get("version"), 0, 0) >= self._REALITY_TOUCH_CONSENT_VERSION
            and isinstance(capabilities, list)
            and capability in capabilities
        )

    def _reality_touch_audio_consented(self, user: dict[str, Any]) -> bool:
        return self._reality_touch_capability_consented(user, "local_audio")

    def _reality_touch_confirmation_prompt(self) -> str:
        return (
            "现实触及会让插件主动调用本机设备。当前版本只使用系统默认音频输出；"
            "未来若增加摄像头能力，仍会要求再次单独确认，不会沿用本次授权。\n"
            "请由用户本人手动输入完整确认信息：\n"
            f"陪伴 现实触及 确认 {self._REALITY_TOUCH_CONFIRMATION_TEXT}"
        )

    @staticmethod
    def _reality_touch_confirmation_valid(text: str) -> bool:
        compact = re.sub(r"[\s，,。.!！;；:：]+", "", str(text or ""))
        if any(marker in compact for marker in ("不同意", "拒绝", "不授权", "取消授权")):
            return False
        return all(
            checks
            for checks in (
                "现实触及" in compact,
                any(marker in compact for marker in ("音频", "音响", "声音输出")),
                "摄像头" in compact,
                any(marker in compact for marker in ("再次确认", "单独确认", "另行确认")),
                any(marker in compact for marker in ("同意", "确认启用", "授权")),
            )
        )

    @staticmethod
    def _wakeup_parse_time(value: Any) -> str:
        text = str(value or "").strip().replace("：", ":")
        match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", text)
        if not match:
            return ""
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return ""
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _wakeup_days(value: Any) -> list[int]:
        if isinstance(value, list):
            raw = value
        else:
            raw = re.split(r"[,，、\s]+", str(value or ""))
        aliases = {
            "一": 0, "周一": 0, "星期一": 0, "1": 0,
            "二": 1, "周二": 1, "星期二": 1, "2": 1,
            "三": 2, "周三": 2, "星期三": 2, "3": 2,
            "四": 3, "周四": 3, "星期四": 3, "4": 3,
            "五": 4, "周五": 4, "星期五": 4, "5": 4,
            "六": 5, "周六": 5, "星期六": 5, "6": 5,
            "日": 6, "天": 6, "周日": 6, "星期日": 6, "周天": 6, "星期天": 6, "7": 6,
        }
        days = []
        for item in raw:
            if isinstance(item, int) and 0 <= item <= 6:
                days.append(item)
                continue
            key = str(item or "").strip()
            if key in {"每天", "全周", "每日"}:
                return list(range(7))
            if key in aliases:
                days.append(aliases[key])
        return sorted(set(days))

    def _wakeup_alarm_for_user(self, user: dict[str, Any]) -> dict[str, Any]:
        alarm = user.get("wakeup_alarm")
        if not isinstance(alarm, dict):
            alarm = {}
            user["wakeup_alarm"] = alarm
        return alarm

    @staticmethod
    def _wakeup_contact_session(alarm: dict[str, Any]) -> dict[str, Any]:
        session = alarm.get("contact_session")
        return session if isinstance(session, dict) else {}

    def _wakeup_delivery_mode(self, alarm: dict[str, Any]) -> str:
        mode = _single_line(alarm.get("delivery_mode"), 32).lower()
        return mode if mode in self._WAKEUP_DELIVERY_MODES else "chat_on_failure"

    def _wakeup_attempt_volume(self, alarm: dict[str, Any], attempt: int) -> int:
        default_volume = self._reality_touch_playback_volume()
        start = _safe_int(alarm.get("playback_volume"), default_volume, 0, 100)
        step = _safe_int(alarm.get("volume_step"), 8, 0, 30)
        maximum = _safe_int(alarm.get("max_volume"), max(start, 70), 0, 100)
        maximum = max(start, maximum)
        return min(maximum, start + max(0, int(attempt) - 1) * step)

    def _wakeup_contact_task_registry(self) -> dict[str, asyncio.Task]:
        registry = getattr(self, "_wakeup_contact_tasks", None)
        if not isinstance(registry, dict):
            registry = {}
            self._wakeup_contact_tasks = registry
        return registry

    def _cancel_wakeup_contact_task(self, user_id: str) -> None:
        task = self._wakeup_contact_task_registry().pop(str(user_id), None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()

    def _stop_wakeup_contact_session(self, user: dict[str, Any], *, status: str = "cancelled") -> None:
        alarm = self._wakeup_alarm_for_user(user)
        session = self._wakeup_contact_session(alarm)
        if _single_line(session.get("status"), 24) in self._WAKEUP_CONTACT_ACTIVE_STATES:
            session.update({"status": status, "completed_at": _now_ts(), "next_attempt_at": 0})
        users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
        if isinstance(users, dict):
            for candidate_id, candidate in users.items():
                if candidate is user:
                    self._cancel_wakeup_contact_task(str(candidate_id))
                    break

    def _launch_wakeup_contact_session(self, user_id: str, session_id: str) -> asyncio.Task | None:
        user_key = str(user_id)
        registry = self._wakeup_contact_task_registry()
        previous = registry.get(user_key)
        if isinstance(previous, asyncio.Task) and not previous.done():
            return previous
        operation = self._run_wakeup_contact_session(user_key, session_id)
        scheduler = getattr(self, "_create_lifecycle_background_task", None)
        if callable(scheduler):
            task = scheduler(operation, label=f"wakeup_contact:{_single_line(user_key, 48)}")
        else:
            try:
                task = asyncio.create_task(operation)
            except RuntimeError:
                operation.close()
                return None
        if isinstance(task, asyncio.Task):
            registry[user_key] = task

            def clear(finished: asyncio.Task) -> None:
                current = self._wakeup_contact_task_registry().get(user_key)
                if current is finished:
                    self._wakeup_contact_task_registry().pop(user_key, None)

            task.add_done_callback(clear)
        return task

    def _wakeup_alarm_status_text(self, user: dict[str, Any]) -> str:
        alarm = self._wakeup_alarm_for_user(user)
        consent_text = "已确认当前音频能力" if self._reality_touch_audio_consented(user) else "尚未完成用户知情确认"
        if not alarm.get("enabled"):
            return (
                "现实触及：当前用户的起床语音关闭\n"
                f"用户授权：{consent_text}\n"
                "设置方式：陪伴 现实触及 07:30\n"
                "先在系统里把蓝牙音响连接并设为默认输出，再用“陪伴 现实触及 测试”试听。"
            )
        days = self._wakeup_days(alarm.get("days")) or list(range(7))
        day_labels = "一二三四五六日"
        day_text = "每天" if len(days) == 7 else "周" + "、".join(day_labels[int(day)] for day in days if 0 <= int(day) <= 6)
        repeat = _safe_int(alarm.get("repeat_count"), 1, 1, 6)
        interval = _safe_int(alarm.get("repeat_interval_seconds"), 20, 5, 300)
        start_volume = _safe_int(alarm.get("playback_volume"), self._reality_touch_playback_volume(), 0, 100)
        max_volume = _safe_int(alarm.get("max_volume"), max(start_volume, 70), 0, 100)
        acknowledgement = "等待醒来确认" if alarm.get("require_acknowledgement", True) else "按次数播放"
        return (
            f"现实触及：起床语音已开启\n时间：{alarm.get('time', '未设置')}（{day_text}）\n"
            f"用户授权：{consent_text}（仅本机音频，不含摄像头）\n"
            f"触达：最多 {repeat} 次，等待 {interval} 秒（{acknowledgement}）\n"
            f"音量：{start_volume}% 起步，最高 {max_volume}%\n"
            f"叫醒偏好：{_single_line(alarm.get('message'), 120) or '未填写'}\n"
            f"话术：{self._WAKEUP_DYNAMIC_MESSAGE_HINT}\n"
            "播放目标：系统默认音频输出（请确认它是已连接的蓝牙音响）。"
        )

    def _wakeup_next_trigger(self, alarm: dict[str, Any], now: datetime | None = None) -> datetime | None:
        alarm_time = self._wakeup_parse_time(alarm.get("time"))
        if not alarm.get("enabled") or not alarm_time:
            return None
        current = now or self._wakeup_now()
        days = self._wakeup_days(alarm.get("days")) or list(range(7))
        hour, minute = (int(part) for part in alarm_time.split(":", 1))
        for offset in range(8):
            candidate = (current + timedelta(days=offset)).replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if candidate.weekday() in days and candidate > current:
                return candidate
        return None

    def _reality_touch_user_snapshot(
        self,
        user_id: str,
        user: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        alarm = user.get("wakeup_alarm") if isinstance(user.get("wakeup_alarm"), dict) else {}
        consent = self._reality_touch_consent(user)
        policy = self._reality_touch_policy(user)
        next_trigger = self._wakeup_next_trigger(alarm, now=now)
        session = self._wakeup_contact_session(alarm)
        label = _single_line(
            user.get("display_name") or user.get("nickname") or user.get("name") or user_id,
            80,
        ) or str(user_id)
        return {
            "user_id": str(user_id),
            "label": label,
            "has_private_route": bool(user.get("umo")),
            "consent": {
                "confirmed": self._reality_touch_audio_consented(user),
                "version": _safe_int(consent.get("version"), 0, 0),
                "confirmed_at": _safe_int(consent.get("confirmed_at"), 0, 0),
                "local_audio": self._reality_touch_capability_consented(user, "local_audio"),
                "camera": self._reality_touch_capability_consented(user, "camera"),
            },
            "policy": {
                "proactive_voice_enabled": bool(policy.get("proactive_voice_enabled")),
                "playback_volume": _safe_int(
                    policy.get("playback_volume"),
                    self._reality_touch_playback_volume(),
                    0,
                    100,
                ),
                "updated_at": _safe_int(policy.get("updated_at"), 0, 0),
            },
            "alarm": {
                "enabled": bool(alarm.get("enabled")),
                "time": self._wakeup_parse_time(alarm.get("time")),
                "days": self._wakeup_days(alarm.get("days")) or list(range(7)),
                "message": _single_line(alarm.get("message"), 240),
                "message_mode": "dynamic",
                "repeat_count": _safe_int(alarm.get("repeat_count"), 1, 1, 6),
                "repeat_interval_seconds": _safe_int(alarm.get("repeat_interval_seconds"), 20, 5, 300),
                "require_acknowledgement": bool(alarm.get("require_acknowledgement", True)),
                "snooze_minutes": _safe_int(alarm.get("snooze_minutes"), 10, 1, 120),
                "playback_volume": _safe_int(
                    alarm.get("playback_volume"),
                    self._reality_touch_playback_volume(),
                    0,
                    100,
                ),
                "volume_step": _safe_int(alarm.get("volume_step"), 8, 0, 30),
                "max_volume": _safe_int(alarm.get("max_volume"), 70, 0, 100),
                "fade_in_ms": _safe_int(alarm.get("fade_in_ms"), 800, 0, 5000),
                "delivery_mode": self._wakeup_delivery_mode(alarm),
                "last_trigger_key": _single_line(alarm.get("last_trigger_key"), 32),
                "next_trigger_at": int(next_trigger.timestamp()) if next_trigger else 0,
                "next_trigger_text": next_trigger.strftime("%m-%d %H:%M") if next_trigger else "",
                "contact_session": {
                    "id": _single_line(session.get("id"), 96),
                    "status": _single_line(session.get("status"), 24),
                    "attempt": _safe_int(session.get("attempt"), 0, 0, 20),
                    "max_attempts": _safe_int(session.get("max_attempts"), 0, 0, 20),
                    "triggered_at": _safe_int(session.get("triggered_at"), 0, 0),
                    "next_attempt_at": _safe_int(session.get("next_attempt_at"), 0, 0),
                    "completed_at": _safe_int(session.get("completed_at"), 0, 0),
                    "last_message": _single_line(session.get("last_message"), 300),
                    "last_volume": _safe_int(session.get("last_volume"), 0, 0, 100),
                    "last_playback_success": bool(session.get("last_playback_success")),
                    "feedback": _single_line(session.get("feedback"), 120),
                },
            },
        }

    def _reality_touch_page_snapshot(self) -> dict[str, Any]:
        users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
        now = self._wakeup_now()
        rows = [
            self._reality_touch_user_snapshot(str(user_id), user, now=now)
            for user_id, user in users.items()
            if isinstance(user, dict)
            and (
                user.get("umo")
                or user.get("reality_touch_consent")
                or user.get("reality_touch_policy")
                or user.get("wakeup_alarm")
            )
        ] if isinstance(users, dict) else []
        rows.sort(
            key=lambda row: (
                not bool(row.get("consent", {}).get("confirmed")),
                not bool(row.get("alarm", {}).get("enabled")),
                str(row.get("label") or row.get("user_id") or ""),
            )
        )
        consented = sum(1 for row in rows if row.get("consent", {}).get("confirmed"))
        proactive_voice = sum(1 for row in rows if row.get("policy", {}).get("proactive_voice_enabled"))
        enabled = sum(1 for row in rows if row.get("alarm", {}).get("enabled"))
        scheduled = sum(1 for row in rows if row.get("alarm", {}).get("next_trigger_at"))
        return {
            "global_enabled": bool(getattr(self, "enable_experimental_bluetooth_wakeup", False)),
            "consent_version": self._REALITY_TOUCH_CONSENT_VERSION,
            "confirmation_command": f"陪伴 现实触及 确认 {self._REALITY_TOUCH_CONFIRMATION_TEXT}",
            "default_message": self._WAKEUP_DEFAULT_MESSAGE,
            "dynamic_message_hint": self._WAKEUP_DYNAMIC_MESSAGE_HINT,
            "audio_output": self._reality_touch_audio_snapshot(),
            "counts": {
                "users": len(rows),
                "consented": consented,
                "proactive_voice": proactive_voice,
                "enabled": enabled,
                "scheduled": scheduled,
            },
            "users": rows,
        }

    def _reality_touch_update_alarm(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        alarm_time = self._wakeup_parse_time(payload.get("time"))
        if not alarm_time:
            raise ValueError("请设置有效的起床时间")
        enabled = bool(payload.get("enabled"))
        if enabled and not self._reality_touch_audio_consented(user):
            raise ValueError("该用户尚未在私聊中完成现实触及知情确认")
        days = self._wakeup_days(payload.get("days"))
        if not days:
            raise ValueError("请至少选择一个重复日期")
        alarm = self._wakeup_alarm_for_user(user)
        alarm.update(
            {
                "enabled": enabled,
                "time": alarm_time,
                "days": days,
                "message": _single_line(payload.get("message"), 240),
                "repeat_count": _safe_int(payload.get("repeat_count"), 1, 1, 6),
                "repeat_interval_seconds": _safe_int(payload.get("repeat_interval_seconds"), 20, 5, 300),
                "require_acknowledgement": bool(payload.get("require_acknowledgement", True)),
                "snooze_minutes": _safe_int(payload.get("snooze_minutes"), 10, 1, 120),
                "playback_volume": _safe_int(
                    payload.get("playback_volume"),
                    self._reality_touch_playback_volume(),
                    0,
                    100,
                ),
                "volume_step": _safe_int(payload.get("volume_step"), 8, 0, 30),
                "max_volume": _safe_int(payload.get("max_volume"), 70, 0, 100),
                "fade_in_ms": _safe_int(payload.get("fade_in_ms"), 800, 0, 5000),
                "delivery_mode": (
                    _single_line(payload.get("delivery_mode"), 32).lower()
                    if _single_line(payload.get("delivery_mode"), 32).lower() in self._WAKEUP_DELIVERY_MODES
                    else "chat_on_failure"
                ),
            }
        )
        alarm["max_volume"] = max(
            _safe_int(alarm.get("playback_volume"), self._reality_touch_playback_volume(), 0, 100),
            _safe_int(alarm.get("max_volume"), 70, 0, 100),
        )
        alarm.pop("last_trigger_key", None)
        if not enabled:
            self._stop_wakeup_contact_session(user)
        return alarm

    def _wakeup_alarm_command(self, user: dict[str, Any], value: str) -> tuple[str, bool]:
        """Apply a chat command. Returns (reply, play_test_now)."""
        text = str(value or "").strip()
        compact = re.sub(r"\s+", "", text).lower()
        alarm = self._wakeup_alarm_for_user(user)
        if not text or compact in {"查看", "状态", "status"}:
            return self._wakeup_alarm_status_text(user), False
        if compact in {"确认", "同意", "授权"}:
            return self._reality_touch_confirmation_prompt(), False
        if text.startswith("确认"):
            confirmation = text[len("确认"):].strip()
            if not self._reality_touch_confirmation_valid(confirmation):
                return (
                    "确认信息不完整，需要同时明确现实触及、本机音频、摄像头需再次单独确认和本人同意。\n"
                    + self._reality_touch_confirmation_prompt()
                ), False
            user["reality_touch_consent"] = {
                "confirmed": True,
                "version": self._REALITY_TOUCH_CONSENT_VERSION,
                "confirmed_at": _now_ts(),
                "confirmation_text": _single_line(confirmation, 300),
                "granted_capabilities": ["local_audio"],
                "camera_granted": False,
            }
            self._save_data_sync()
            return "现实触及知情确认已记录。当前只授权本机音频能力，未授权摄像头。", False
        if compact in {"撤销确认", "撤销授权", "取消确认", "取消授权"}:
            user.pop("reality_touch_consent", None)
            alarm["enabled"] = False
            self._stop_wakeup_contact_session(user)
            self._save_data_sync()
            return "已撤销现实触及授权，并关闭当前用户的起床语音。", False
        if compact in {"关闭", "取消", "停用", "off", "disable"}:
            alarm["enabled"] = False
            self._stop_wakeup_contact_session(user)
            self._save_data_sync()
            return "已关闭现实触及的起床语音。", False
        if not self._reality_touch_audio_consented(user):
            return self._reality_touch_confirmation_prompt(), False
        if compact in {"测试", "试听", "test"}:
            if not alarm.get("enabled") or not self._wakeup_parse_time(alarm.get("time")):
                return "请先设置时间，例如：陪伴 现实触及 07:30，然后再测试。", False
            return "正在通过系统默认音频输出试听起床语音。", True

        parts = text.split(maxsplit=1)
        alarm_time = self._wakeup_parse_time(parts[0])
        if not alarm_time:
            return (
                "时间格式不对。请使用：陪伴 现实触及 07:30\n"
                "也可以：陪伴 现实触及 关闭 / 查看 / 测试。"
            ), False
        alarm["time"] = alarm_time
        alarm["enabled"] = True
        alarm.setdefault("days", list(range(7)))
        alarm.setdefault("message", "")
        alarm.setdefault("repeat_count", 1)
        alarm.setdefault("repeat_interval_seconds", 20)
        alarm.setdefault("require_acknowledgement", True)
        alarm.setdefault("snooze_minutes", 10)
        alarm.setdefault("volume_step", 8)
        alarm.setdefault("max_volume", 70)
        alarm.setdefault("fade_in_ms", 800)
        alarm.setdefault("delivery_mode", "chat_on_failure")
        if len(parts) == 2:
            option = parts[1].strip()
            if option:
                day_part, _, message = option.partition(" ")
                parsed_days = self._wakeup_days(day_part)
                if parsed_days:
                    alarm["days"] = parsed_days
                    if message.strip():
                        alarm["message"] = _single_line(message, 240)
                else:
                    alarm["message"] = _single_line(option, 240)
        alarm.pop("last_trigger_key", None)
        self._save_data_sync()
        return self._wakeup_alarm_status_text(user), False

    def _wakeup_now(self) -> datetime:
        timezone_name = _single_line(getattr(self, "environment_perception_timezone", "Asia/Shanghai"), 64)
        try:
            return datetime.now(zoneinfo.ZoneInfo(timezone_name or "Asia/Shanghai"))
        except Exception:
            return datetime.now().astimezone()

    async def _generate_wakeup_alarm_message(
        self,
        user: dict[str, Any],
        alarm: dict[str, Any],
        *,
        test: bool = False,
    ) -> str:
        """Create one in-character utterance for this firing; stored text is only a preference."""
        llm_call = getattr(self, "_llm_call", None)
        if not callable(llm_call):
            return self._WAKEUP_DEFAULT_MESSAGE

        umo = _single_line(user.get("umo"), 240)
        name = _single_line(
            user.get("nickname")
            or user.get("last_display_name")
            or user.get("display_name")
            or user.get("name"),
            48,
        )
        persona = ""
        persona_resolver = getattr(self, "_resolve_proactive_persona_prompt", None)
        if callable(persona_resolver):
            try:
                persona = str(await persona_resolver(user, umo=umo) or "").strip()
            except TypeError:
                try:
                    persona = str(await persona_resolver(user) or "").strip()
                except Exception:
                    persona = ""
            except Exception:
                persona = ""

        relationship = ""
        relationship_formatter = getattr(self, "_format_proactive_relationship_fact", None)
        if callable(relationship_formatter):
            try:
                relationship = str(relationship_formatter(user) or "").strip()
            except Exception:
                relationship = ""

        history = ""
        history_getter = getattr(self, "_recent_private_conversation_for_proactive_review", None)
        if callable(history_getter):
            try:
                history = str(await history_getter(user, limit=8) or "").strip()
            except Exception:
                history = ""
        if not history:
            recent_lines = []
            last_bot = _single_line(user.get("last_companion_message"), 180)
            last_user = _single_line(user.get("last_user_message"), 180)
            if last_bot:
                recent_lines.append(f"Bot：{last_bot}")
            if last_user:
                recent_lines.append(f"用户：{last_user}")
            history = "\n".join(recent_lines)

        now = self._wakeup_now()
        weekday = "一二三四五六日"[now.weekday()]
        preference = _single_line(alarm.get("message"), 240)
        attempt = _safe_int(alarm.get("_contact_attempt"), 1, 1, 20)
        max_attempts = _safe_int(alarm.get("_contact_max_attempts"), 1, 1, 20)
        previous_messages = alarm.get("_contact_previous_messages")
        previous_text = "\n".join(
            f"- {_single_line(item, 240)}"
            for item in (previous_messages if isinstance(previous_messages, list) else [])[-4:]
            if _single_line(item, 240)
        )
        acknowledgement_hint = (
            "可以自然邀请对方醒来后回一句，让系统停止后续提醒，但不要把回应写成命令或施加压力。"
            if bool(alarm.get("require_acknowledgement", True))
            else "不必要求对方回复。"
        )
        prompt = (
            "请为一次现实设备上的起床提醒写出此刻真正会对用户说的话。\n"
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}，周{weekday}\n"
            f"这是本轮第 {attempt}/{max_attempts} 次触达。\n"
            f"用户称呼：{name or '没有可靠称呼，直接自然地说“你”'}\n"
            f"关系状态：{relationship or '没有额外关系资料，保持自然、低压力'}\n"
            f"用户设置的叫醒偏好或补充要求：{preference or '无，由你根据人格和语境自然发挥'}\n"
            f"本轮此前已经说过的话：\n{previous_text or '无，这是第一次触达'}\n"
            f"最近对话：\n{history[-2400:] or '没有可用的最近对话，不要自行编造昨晚或今天的经历'}\n\n"
            "只输出最终说出口的一到两句短话，不要标题、引号、Markdown、括号动作、TTS 标签或解释。"
            "目标是自然地把对方叫醒，措辞应每天有变化，并贴合人格和关系距离；设置内容只提供意图和事实，"
            "需要融入当下重新表达，不要逐字复读。后续触达要换一种说法，可以比前一次更明确一点，但仍保持温和。"
            "可以有温度、惦记或一点生活感，但不要像通知、客服或健康打卡。"
            f"{acknowledgement_hint}"
            "不要声称看见、监听或确认了用户正在睡觉或已经醒来，不编造天气、日程和昨晚发生的事；"
            "不要命令、训斥、内疚施压，也不要制造紧迫恐慌。"
        )
        system_prompt = (
            "你正在延续下面的人格，以这个人本来的口吻说一句真实、克制的叫醒话。"
            "人格是表达依据，不要复述或解释人格设定。\n\n"
            + (persona[:6000] if persona else "保持自然、有生活感的陪伴者口吻。")
        )
        provider_id = None
        provider_picker = getattr(self, "_task_provider", None)
        if callable(provider_picker):
            try:
                provider_id = provider_picker(
                    getattr(self, "voice_prompt_provider_id", ""),
                    getattr(self, "mai_style_provider_id", ""),
                    getattr(self, "fast_response_provider_id", ""),
                    getattr(self, "llm_provider_id", ""),
                ) or None
            except Exception:
                provider_id = None
        try:
            raw = await llm_call(
                prompt,
                max_tokens=160,
                provider_id=provider_id,
                task="wakeup_alarm_message",
                system_prompt=system_prompt,
                timeout_key="VOICE_PROMPT_PROVIDER_ID",
            )
        except Exception as exc:
            logger.warning("[PrivateCompanion] 起床提醒动态话术生成失败，使用兜底文本: %s", _single_line(exc, 160))
            return self._WAKEUP_DEFAULT_MESSAGE

        message = str(raw or "").strip()
        message = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", message, flags=re.IGNORECASE)
        message = re.sub(r"^(?:最终(?:话术|文本|回复)|话术|回复|输出)\s*[:：]\s*", "", message)
        message = re.sub(r"<[^>]{1,80}>", "", message)
        message = _single_line(message.strip().strip('"\'“”‘’'), 500)
        if not message:
            return self._WAKEUP_DEFAULT_MESSAGE
        return message

    async def _play_wakeup_alarm(
        self,
        user: dict[str, Any],
        alarm: dict[str, Any],
        *,
        test: bool = False,
        volume: Any = None,
    ) -> bool:
        message = await self._generate_wakeup_alarm_message(user, alarm, test=test)
        repeat = 1 if test else _safe_int(alarm.get("repeat_count"), 1, 1, 6)
        interval = _safe_int(alarm.get("repeat_interval_seconds"), 20, 5, 300)
        playback_kwargs = {
            "repeat": repeat,
            "interval": interval,
            "fade_in_ms": _safe_int(alarm.get("fade_in_ms"), 800, 0, 5000),
            "source": "wakeup_test" if test else "wakeup_alarm",
        }
        if volume is not None:
            playback_kwargs["volume"] = volume
        played = await self._play_reality_touch_text(message, **playback_kwargs)
        if played:
            logger.info("[PrivateCompanion] 起床提醒场景已播放: test=%s repeat=%s", test, repeat)
        return played

    async def _send_wakeup_chat_copy(self, user: dict[str, Any], message: str) -> bool:
        umo = _single_line(user.get("umo"), 240)
        direct_sender = getattr(self, "_send_chain_components", None)
        sender = getattr(self, "_send_proactive_message_chain", None)
        if not umo or (not callable(direct_sender) and not callable(sender)):
            return False
        try:
            if callable(direct_sender):
                from astrbot.api.message_components import Plain

                return bool(await direct_sender(umo, [Plain(message)]))
            outcome = await sender(umo, message)
            if isinstance(outcome, bool):
                return outcome
            return bool(getattr(outcome, "delivered", False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[PrivateCompanion] 起床提醒聊天副本发送失败: %s", _single_line(exc, 160))
            return False

    async def _run_wakeup_contact_session(self, user_id: str, session_id: str) -> None:
        """Run cancellable attempts until the user responds or the attempt budget is exhausted."""
        while True:
            users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
            user = users.get(str(user_id)) if isinstance(users, dict) else None
            if not isinstance(user, dict):
                return
            alarm = user.get("wakeup_alarm") if isinstance(user.get("wakeup_alarm"), dict) else {}
            session = self._wakeup_contact_session(alarm)
            if _single_line(session.get("id"), 96) != session_id:
                return
            status = _single_line(session.get("status"), 24)
            if status not in self._WAKEUP_CONTACT_ACTIVE_STATES:
                return
            now_ts = _now_ts()
            next_attempt_at = _safe_int(session.get("next_attempt_at"), 0, 0)
            if next_attempt_at > now_ts:
                await asyncio.sleep(max(0.2, next_attempt_at - now_ts))
                continue

            attempt = _safe_int(session.get("attempt"), 0, 0, 20) + 1
            maximum = _safe_int(
                session.get("max_attempts"),
                _safe_int(alarm.get("repeat_count"), 1, 1, 6),
                1,
                6,
            )
            if attempt > maximum:
                session.update({"status": "exhausted", "completed_at": _now_ts(), "next_attempt_at": 0})
                self._schedule_data_save(delay=0.2)
                return

            previous = session.get("messages") if isinstance(session.get("messages"), list) else []
            alarm_for_attempt = copy.deepcopy(alarm)
            alarm_for_attempt["_contact_attempt"] = attempt
            alarm_for_attempt["_contact_max_attempts"] = maximum
            alarm_for_attempt["_contact_previous_messages"] = list(previous)
            session.update({"status": "playing", "attempt": attempt, "last_attempt_at": _now_ts()})
            self._schedule_data_save(delay=0.2)

            message = await self._generate_wakeup_alarm_message(user, alarm_for_attempt)
            volume = self._wakeup_attempt_volume(alarm, attempt)
            played = await self._play_reality_touch_text(
                message,
                repeat=1,
                interval=_safe_int(alarm.get("repeat_interval_seconds"), 20, 5, 300),
                volume=volume,
                fade_in_ms=_safe_int(alarm.get("fade_in_ms"), 800, 0, 5000),
                source="wakeup_alarm",
            )
            delivery_mode = self._wakeup_delivery_mode(alarm)
            chat_sent = False
            if delivery_mode == "audio_and_chat" or (delivery_mode == "chat_on_failure" and not played):
                chat_sent = await self._send_wakeup_chat_copy(user, message)

            current = self._wakeup_contact_session(alarm)
            if _single_line(current.get("id"), 96) != session_id:
                return
            if _single_line(current.get("status"), 24) not in self._WAKEUP_CONTACT_ACTIVE_STATES:
                return
            messages = current.get("messages") if isinstance(current.get("messages"), list) else []
            messages.append(message)
            current.update(
                {
                    "status": "pending",
                    "messages": messages[-6:],
                    "last_message": message,
                    "last_volume": volume,
                    "last_playback_success": played,
                    "last_chat_success": chat_sent,
                }
            )
            if attempt >= maximum or not bool(alarm.get("require_acknowledgement", True)):
                if attempt >= maximum:
                    current.update({"status": "exhausted", "completed_at": _now_ts(), "next_attempt_at": 0})
                    self._schedule_data_save(delay=0.2)
                    return
            interval = _safe_int(alarm.get("repeat_interval_seconds"), 20, 5, 300)
            current["next_attempt_at"] = _now_ts() + interval
            self._schedule_data_save(delay=0.2)

    async def _classify_wakeup_feedback(self, text: str, default_snooze: int) -> tuple[str, int]:
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", str(text or "")).lower()
        if not compact:
            return "other", 0
        duration = re.search(r"(\d{1,3})\s*(小时|分钟|分)(?:钟)?后", str(text or ""))
        if duration and any(marker in compact for marker in ("叫", "提醒", "喊", "再睡", "晚点")):
            value = int(duration.group(1)) * (60 if duration.group(2) == "小时" else 1)
            return "snooze", max(1, min(120, value))
        if any(marker in compact for marker in ("稍后叫", "等会叫", "过会叫", "晚点叫", "再睡会", "还没醒", "没醒呢")):
            return "snooze", default_snooze
        if any(marker in compact for marker in ("今天不用叫", "今天别叫", "停止这次", "这次取消", "不用再叫")):
            return "stop", 0
        explicit_awake = re.fullmatch(
            r"(?:好|好啦|嗯|恩)?(?:我)?(?:已经)?(?:醒了|醒啦|起了|起来了|起床了)(?:呀|啊|哦|呢|哈|谢谢)?",
            compact,
        )
        if explicit_awake or compact in {"不用叫了", "别叫了我醒了", "不用提醒了我醒了"}:
            return "awake", 0

        llm_call = getattr(self, "_llm_call", None)
        if not callable(llm_call) or len(compact) > 120:
            return "other", 0
        prompt = (
            "判断下面这句用户消息是否是在回应刚刚的起床提醒。只输出一个结果："
            "awake、snooze、stop 或 other。awake=明确已经醒来或起床；"
            "snooze=明确还没醒、想稍后再叫；stop=只取消今天这轮提醒；"
            "other=普通聊天、含糊表达或无法确定。宁可输出 other，不要把普通聊天误判为控制指令。\n"
            f"用户消息：{_single_line(text, 240)}"
        )
        try:
            result = _single_line(
                await llm_call(prompt, max_tokens=12, task="wakeup_feedback_intent"),
                24,
            ).lower()
        except Exception:
            return "other", 0
        if result.startswith("awake"):
            return "awake", 0
        if result.startswith("snooze"):
            return "snooze", default_snooze
        if result.startswith("stop"):
            return "stop", 0
        return "other", 0

    async def _maybe_handle_wakeup_feedback(
        self,
        event: Any,
        user_id: str,
        user: dict[str, Any],
        text: str,
    ) -> bool:
        alarm = user.get("wakeup_alarm") if isinstance(user.get("wakeup_alarm"), dict) else {}
        session = self._wakeup_contact_session(alarm)
        if _single_line(session.get("status"), 24) not in self._WAKEUP_CONTACT_ACTIVE_STATES:
            return False
        snooze_default = _safe_int(alarm.get("snooze_minutes"), 10, 1, 120)
        intent, snooze_minutes = await self._classify_wakeup_feedback(text, snooze_default)
        if intent == "other":
            return False

        now_ts = _now_ts()
        if intent == "snooze":
            minutes = max(1, min(120, snooze_minutes or snooze_default))
            session.update(
                {
                    "status": "snoozed",
                    "feedback": _single_line(text, 120),
                    "feedback_at": now_ts,
                    "next_attempt_at": now_ts + minutes * 60,
                }
            )
            reply = f"好，{minutes} 分钟后再叫你。这期间不会继续播放。"
        elif intent == "awake":
            session.update(
                {
                    "status": "acknowledged",
                    "feedback": _single_line(text, 120),
                    "feedback_at": now_ts,
                    "completed_at": now_ts,
                    "next_attempt_at": 0,
                }
            )
            reply = "好，收到你已经醒来的确认了，今天这轮提醒已经停止。"
        else:
            session.update(
                {
                    "status": "cancelled",
                    "feedback": _single_line(text, 120),
                    "feedback_at": now_ts,
                    "completed_at": now_ts,
                    "next_attempt_at": 0,
                }
            )
            reply = "好，今天这轮提醒已取消，不影响之后设定的日期。"

        self._cancel_wakeup_contact_task(user_id)
        self._schedule_data_save(delay=0.1)
        if intent == "snooze":
            self._launch_wakeup_contact_session(user_id, _single_line(session.get("id"), 96))
        replier = getattr(self, "_reply", None)
        if callable(replier):
            await replier(event, reply)
        try:
            event.stop_event()
        except Exception:
            pass
        return True

    async def _run_wakeup_alarm_tick(self) -> None:
        if not bool(getattr(self, "enable_experimental_bluetooth_wakeup", False)):
            return
        users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
        if not isinstance(users, dict):
            return
        now = self._wakeup_now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        check_window = max(90, _safe_int(getattr(self, "check_interval_seconds", 60), 60, 30) + 15)
        for user_id, user in list(users.items()):
            if not isinstance(user, dict) or not user.get("umo"):
                continue
            alarm = user.get("wakeup_alarm")
            if not isinstance(alarm, dict):
                continue
            if not self._reality_touch_audio_consented(user):
                continue
            session = self._wakeup_contact_session(alarm)
            session_status = _single_line(session.get("status"), 24)
            session_id = _single_line(session.get("id"), 96)
            session_due = _safe_int(session.get("next_attempt_at"), 0, 0) <= _now_ts()
            running_task = self._wakeup_contact_task_registry().get(str(user_id))
            if (
                session_id
                and session_status in self._WAKEUP_CONTACT_ACTIVE_STATES
                and session_due
                and not (isinstance(running_task, asyncio.Task) and not running_task.done())
            ):
                self._launch_wakeup_contact_session(str(user_id), session_id)
            alarm_time = self._wakeup_parse_time(alarm.get("time"))
            if not alarm.get("enabled") or not alarm_time:
                continue
            hour, minute = (int(part) for part in alarm_time.split(":", 1))
            scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not 0 <= (now - scheduled_at).total_seconds() <= check_window:
                continue
            days = self._wakeup_days(alarm.get("days")) or list(range(7))
            if now.weekday() not in days or alarm.get("last_trigger_key") == minute_key:
                continue
            alarm["last_trigger_key"] = minute_key
            session_id = f"{user_id}:{now.strftime('%Y%m%d%H%M')}"
            alarm["contact_session"] = {
                "id": session_id,
                "status": "pending",
                "attempt": 0,
                "max_attempts": _safe_int(alarm.get("repeat_count"), 1, 1, 6),
                "triggered_at": _now_ts(),
                "next_attempt_at": _now_ts(),
                "messages": [],
            }
            self._schedule_data_save(delay=0.2)
            task = self._launch_wakeup_contact_session(str(user_id), session_id)
            if task is None:
                await self._run_wakeup_contact_session(str(user_id), session_id)

    async def _test_wakeup_alarm(self, user: dict[str, Any]) -> None:
        alarm = self._wakeup_alarm_for_user(user)
        await self._play_wakeup_alarm(copy.deepcopy(user), copy.deepcopy(alarm), test=True)

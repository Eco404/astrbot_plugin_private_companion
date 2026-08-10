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
        return (
            f"现实触及：起床语音已开启\n时间：{alarm.get('time', '未设置')}（{day_text}）\n"
            f"用户授权：{consent_text}（仅本机音频，不含摄像头）\n"
            f"重复：{repeat} 次，间隔 {interval} 秒\n"
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
                "last_trigger_key": _single_line(alarm.get("last_trigger_key"), 32),
                "next_trigger_at": int(next_trigger.timestamp()) if next_trigger else 0,
                "next_trigger_text": next_trigger.strftime("%m-%d %H:%M") if next_trigger else "",
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
            }
        )
        alarm.pop("last_trigger_key", None)
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
            self._save_data_sync()
            return "已撤销现实触及授权，并关闭当前用户的起床语音。", False
        if compact in {"关闭", "取消", "停用", "off", "disable"}:
            alarm["enabled"] = False
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
        prompt = (
            "请为一次现实设备上的起床提醒写出此刻真正会对用户说的话。\n"
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}，周{weekday}\n"
            f"用户称呼：{name or '没有可靠称呼，直接自然地说“你”'}\n"
            f"关系状态：{relationship or '没有额外关系资料，保持自然、低压力'}\n"
            f"用户设置的叫醒偏好或补充要求：{preference or '无，由你根据人格和语境自然发挥'}\n"
            f"最近对话：\n{history[-2400:] or '没有可用的最近对话，不要自行编造昨晚或今天的经历'}\n\n"
            "只输出最终说出口的一到两句短话，不要标题、引号、Markdown、括号动作、TTS 标签或解释。"
            "目标是自然地把对方叫醒，措辞应每天有变化，并贴合人格和关系距离；设置内容只提供意图和事实，"
            "需要融入当下重新表达，不要逐字复读。可以有温度、惦记或一点生活感，但不要像通知、客服或健康打卡。"
            "不要声称看见、监听或确认了用户正在睡觉或已经醒来，不编造天气、日程和昨晚发生的事；"
            "不要命令、训斥、内疚施压，也不要要求立即回应。"
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
        playback_kwargs = {"repeat": repeat, "interval": interval}
        if volume is not None:
            playback_kwargs["volume"] = volume
        played = await self._play_reality_touch_text(message, **playback_kwargs)
        if played:
            logger.info("[PrivateCompanion] 起床提醒场景已播放: test=%s repeat=%s", test, repeat)
        return played

    async def _run_wakeup_alarm_tick(self) -> None:
        if not bool(getattr(self, "enable_experimental_bluetooth_wakeup", False)):
            return
        users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
        if not isinstance(users, dict):
            return
        now = self._wakeup_now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        check_window = max(90, _safe_int(getattr(self, "check_interval_seconds", 60), 60, 30) + 15)
        for user in list(users.values()):
            if not isinstance(user, dict) or not user.get("umo"):
                continue
            alarm = user.get("wakeup_alarm")
            if not isinstance(alarm, dict):
                continue
            if not self._reality_touch_audio_consented(user):
                continue
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
            self._schedule_data_save(delay=0.2)
            operation = self._play_wakeup_alarm(copy.deepcopy(user), copy.deepcopy(alarm))
            scheduler = getattr(self, "_create_lifecycle_background_task", None)
            if callable(scheduler):
                scheduler(operation, label="wakeup_alarm_playback")
            else:
                await operation

    async def _test_wakeup_alarm(self, user: dict[str, Any]) -> None:
        alarm = self._wakeup_alarm_for_user(user)
        await self._play_wakeup_alarm(copy.deepcopy(user), copy.deepcopy(alarm), test=True)

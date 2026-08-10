# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_private_companion.daily_state import DailyStateMixin


class ReminderRoutingHarness(DailyStateMixin):
    def __init__(self) -> None:
        self.enable_llm_timer_scheduling = True
        self.enable_experimental_bluetooth_wakeup = True
        self._schedule_llm_timer = AsyncMock()
        self._schedule_reality_touch_official_reminder = AsyncMock(return_value=True)

    @staticmethod
    def _reality_touch_audio_consented(_user) -> bool:
        return True

    @staticmethod
    def _private_user_role(_user) -> str:
        return "owner"

    @staticmethod
    def _environment_fromtimestamp(value: float) -> datetime:
        return datetime.fromtimestamp(float(value))

    @staticmethod
    def _proactive_quota_policy(user: dict) -> dict:
        tier = int(user.get("test_quota_tier", 3))
        return {"tier": tier, "label": f"测试档位{tier}"}


class ReminderRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.plugin = ReminderRoutingHarness()

    def test_timer_prompt_defines_memo_and_official_tool_boundaries(self):
        instruction = self.plugin._format_timer_scheduling_instruction({})
        self.assertIn("优先调用该工具", instruction)
        self.assertIn("只能选择 `future_task` 或 `<timer>` 其中一种", instruction)
        self.assertIn("应使用 `pc_manage_memo`", instruction)
        self.assertIn("动作回访", instruction)
        self.assertIn('"delivery":"reality_touch"', instruction)
        self.assertIn("取消现实触及提醒时必须保留交付类型", instruction)

    def test_reality_touch_directive_keeps_delivery_metadata(self):
        payload = self.plugin._parse_timer_directive(
            '{"time":"2027-01-15 08:30:00","delivery":"reality_touch",'
            '"delivery_mode":"audio_only","playback_volume":42,"fade_in_ms":600,"topic":"喝水"}'
        )

        self.assertIsNotNone(payload)
        self.assertEqual("reality_touch", payload["delivery"])
        self.assertEqual("audio_only", payload["delivery_mode"])
        self.assertEqual(42, payload["playback_volume"])
        self.assertEqual(600, payload["fade_in_ms"])

    async def test_reality_touch_reminder_routes_to_official_reality_touch_scheduler(self):
        payload = {
            "scheduled_ts": 1_800_000_000,
            "delivery": "reality_touch",
            "topic": "喝水",
        }

        result = await self.plugin._schedule_llm_timer_after_response_dedup(
            SimpleNamespace(),
            SimpleNamespace(tools_call_name=[]),
            "owner",
            payload,
            source_text="用现实触及提醒我喝水",
            visible_text="好。",
            trigger_umo="aiocqhttp:FriendMessage:owner",
        )

        self.assertEqual("reality_touch_official", result)
        self.plugin._schedule_reality_touch_official_reminder.assert_awaited_once()
        self.plugin._schedule_llm_timer.assert_not_awaited()

    def test_activity_followup_policy_adapts_to_all_quota_tiers(self):
        expected = {
            1: (1, 15),
            2: (1, 8),
            3: (2, 3),
            4: (2, 0),
            5: (2, 0),
        }
        for tier, (max_intensity, buffer_minutes) in expected.items():
            with self.subTest(tier=tier):
                policy = self.plugin._activity_followup_quota_policy(
                    {"test_quota_tier": tier, "style": "自然陪伴"}
                )
                self.assertEqual(max_intensity, policy["max_intensity"])
                self.assertEqual(buffer_minutes, policy["completion_buffer_minutes"])

        strong_policy = self.plugin._activity_followup_quota_policy(
            {"test_quota_tier": 5, "style": "黏人，会轻轻查岗"}
        )
        self.assertEqual(3, strong_policy["max_intensity"])

    def test_activity_followup_policy_stays_gentle_for_friend_or_ignored_user(self):
        self.plugin._private_user_role = lambda _user: "friend"
        friend_policy = self.plugin._activity_followup_quota_policy(
            {"test_quota_tier": 5, "style": "黏人，会轻轻查岗"}
        )
        self.assertEqual(1, friend_policy["max_intensity"])

        self.plugin._private_user_role = lambda _user: "owner"
        ignored_policy = self.plugin._activity_followup_quota_policy(
            {"test_quota_tier": 5, "style": "黏人，会轻轻查岗", "ignored_streak": 1}
        )
        self.assertEqual(1, ignored_policy["max_intensity"])
        self.assertGreaterEqual(ignored_policy["completion_buffer_minutes"], 10)

    async def test_saved_memo_reminder_strips_timer_but_does_not_schedule(self):
        text = '便签记好了。<timer>{"action":"cancel"}</timer>'
        cleaned, payloads = self.plugin._extract_timer_directives(text)
        self.assertEqual("便签记好了。", cleaned)
        self.assertEqual(1, len(payloads))

        event = SimpleNamespace(private_companion_memo_reminder_saved=True)
        result = await self.plugin._schedule_llm_timer_after_response_dedup(
            event,
            SimpleNamespace(tools_call_name=[]),
            "owner",
            payloads[0],
            source_text="帮我记一下明天交材料",
            visible_text=cleaned,
            trigger_umo="aiocqhttp:FriendMessage:owner",
        )
        self.assertEqual("memo_reminder", result)
        self.plugin._schedule_llm_timer.assert_not_awaited()

    async def test_generic_reminder_still_uses_timer_path(self):
        payload = {"scheduled_ts": 1_800_000_000, "topic": "交材料"}
        result = await self.plugin._schedule_llm_timer_after_response_dedup(
            SimpleNamespace(),
            SimpleNamespace(tools_call_name=[]),
            "owner",
            payload,
            source_text="明天提醒我交材料",
            visible_text="好，明天提醒你。",
        )
        self.assertEqual("scheduled", result)
        self.plugin._schedule_llm_timer.assert_awaited_once()

    async def test_activity_followup_still_uses_timer_path(self):
        payload = {
            "scheduled_ts": 1_800_000_000,
            "reason": "activity_followup",
            "activity": "洗澡",
        }
        result = await self.plugin._schedule_llm_timer_after_response_dedup(
            SimpleNamespace(),
            SimpleNamespace(tools_call_name=[]),
            "owner",
            payload,
            source_text="我去洗澡了",
            visible_text="去吧。",
        )
        self.assertEqual("scheduled", result)
        scheduled_payload = self.plugin._schedule_llm_timer.await_args.args[1]
        self.assertEqual("activity_followup", scheduled_payload["reason"])

    async def test_future_task_tool_call_suppresses_timer_for_string_and_list_names(self):
        payload = {"scheduled_ts": 1_800_000_000, "topic": "交材料"}
        for names in ("future_task", ["future_task"]):
            with self.subTest(names=names):
                self.plugin._schedule_llm_timer.reset_mock()
                result = await self.plugin._schedule_llm_timer_after_response_dedup(
                    SimpleNamespace(),
                    SimpleNamespace(tools_call_name=names),
                    "owner",
                    payload,
                    source_text="明天提醒我交材料",
                    visible_text="已经安排好了。",
                )
                self.assertEqual("official_task", result)
                self.plugin._schedule_llm_timer.assert_not_awaited()

    async def test_successful_future_task_result_sets_reliable_dedup_marker(self):
        event = SimpleNamespace()
        tool = SimpleNamespace(name="future_task")
        tool_result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text="Scheduled future task job-123456 (reminder) one-time at tomorrow.")],
        )
        self.assertTrue(
            self.plugin._record_future_task_result(
                event,
                tool,
                {"action": "create"},
                tool_result,
            )
        )

        result = await self.plugin._schedule_llm_timer_after_response_dedup(
            event,
            SimpleNamespace(tools_call_name=[]),
            "owner",
            {"scheduled_ts": 1_800_000_000, "topic": "交材料"},
            source_text="明天提醒我交材料",
            visible_text="已经安排好了。",
        )
        self.assertEqual("official_task", result)
        self.plugin._schedule_llm_timer.assert_not_awaited()

    async def test_failed_future_task_result_does_not_block_timer_fallback(self):
        event = SimpleNamespace()
        tool = SimpleNamespace(name="future_task")
        tool_result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text="error: failed to schedule task due to invalid configuration.")],
        )
        self.assertFalse(
            self.plugin._record_future_task_result(
                event,
                tool,
                {"action": "create"},
                tool_result,
            )
        )

        result = await self.plugin._schedule_llm_timer_after_response_dedup(
            event,
            SimpleNamespace(tools_call_name=["future_task"]),
            "owner",
            {"scheduled_ts": 1_800_000_000, "topic": "交材料"},
            source_text="明天提醒我交材料",
            visible_text="官方定时失败，改用临时预约。",
        )
        self.assertEqual("scheduled", result)
        self.plugin._schedule_llm_timer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

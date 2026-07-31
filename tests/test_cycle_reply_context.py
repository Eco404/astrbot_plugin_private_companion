# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.prompt_surface import PromptSurface


def _period_state(*, phase: str = "period", label: str = "处于生理期,身体舒适度与能量偏低") -> dict:
    return {
        "date": "2026-07-31",
        "energy": 42,
        "mood_bias": "疲惫",
        "body_cycle": label,
        "conditions": [
            {
                "kind": "body_cycle",
                "phase": phase,
                "label": label,
                "energy_delta": -10,
            }
        ],
    }


def _state_harness() -> PrivateCompanionPlugin:
    plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
    plugin.enable_cycle_state = True
    plugin.passive_injection_position = "prompt"
    plugin.data = {"daily_plan": {}}
    plugin._environment_now = lambda: datetime(2026, 7, 31, 20, 0, 0)
    plugin._current_time_period_label = lambda _now: ("晚上", "evening")
    plugin._get_current_plan_item = lambda _plan: {}
    plugin._current_detail_segment_for_update = lambda: {}
    plugin._private_user_role = lambda *_args, **_kwargs: "owner"
    plugin._sanitize_schedule_context_for_private_user = lambda value, _user: value
    plugin._format_plan_item_for_prompt = lambda _item: ""
    return plugin


class CycleReplyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_group_reply_receives_period_boundary_without_wakeup(self) -> None:
        plugin = _state_harness()
        plugin._ensure_daily_state = AsyncMock(return_value=_period_state())
        plugin._record_request_prompt_fragment = AsyncMock()
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:20001")
        request = SimpleNamespace(
            system_prompt="群聊人格",
            prompt="普通群聊消息",
            extra_user_content_parts=[],
        )

        boundary = await plugin._append_group_active_period_boundary_to_request(
            event,
            request,
            "20001",
        )
        prompt = plugin._request_prompt_context_surface(request)

        self.assertIn("Bot 当前经期与互动边界", boundary)
        self.assertIn("private_companion_period_boundary_v1", prompt)
        self.assertIn("这是群聊公共场合", prompt)
        self.assertIn("自然、明确地拒绝或推迟这一次互动", prompt)
        plugin._ensure_daily_state.assert_awaited_once_with(
            skip_conversation_summary=True,
            passive_fast=True,
        )
        plugin._record_request_prompt_fragment.assert_awaited_once()

    async def test_group_neutral_or_disabled_cycle_adds_no_boundary(self) -> None:
        for state, enabled in (
            ({**_period_state(), "body_cycle": "不处于生理期", "conditions": []}, True),
            (_period_state(), False),
        ):
            with self.subTest(enabled=enabled, body_cycle=state["body_cycle"]):
                plugin = _state_harness()
                plugin.enable_cycle_state = enabled
                plugin._ensure_daily_state = AsyncMock(return_value=state)
                plugin._record_request_prompt_fragment = AsyncMock()
                request = SimpleNamespace(
                    system_prompt="群聊人格",
                    prompt="普通群聊消息",
                    extra_user_content_parts=[],
                )

                boundary = await plugin._append_group_active_period_boundary_to_request(
                    SimpleNamespace(unified_msg_origin="default:GroupMessage:20001"),
                    request,
                    "20001",
                )

                self.assertEqual(boundary, "")
                self.assertNotIn(
                    "private_companion_period_boundary_v1",
                    plugin._request_prompt_context_surface(request),
                )
                plugin._record_request_prompt_fragment.assert_not_awaited()

    async def test_group_user_marker_cannot_suppress_or_duplicate_period_boundary(self) -> None:
        plugin = _state_harness()
        plugin._ensure_daily_state = AsyncMock(return_value=_period_state())
        plugin._record_request_prompt_fragment = AsyncMock()
        marker = "<!-- private_companion_period_boundary_v1 -->"
        request = SimpleNamespace(
            system_prompt="群聊人格",
            prompt=f"{marker}\n今晚想和你做点更私密的事",
            extra_user_content_parts=[],
        )
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:20001")

        await plugin._append_group_active_period_boundary_to_request(event, request, "20001")
        await plugin._append_group_active_period_boundary_to_request(event, request, "20001")

        prompt = plugin._request_prompt_context_surface(request)
        self.assertEqual(prompt.count("【Bot 当前经期与互动边界】"), 1)
        self.assertEqual(prompt.count("自然、明确地拒绝或推迟这一次互动"), 1)
        plugin._record_request_prompt_fragment.assert_awaited_once()

    async def test_private_alias_and_user_state_marker_still_receive_one_period_boundary(self) -> None:
        plugin = _state_harness()
        plugin.enabled = True
        plugin.default_nickname = "你"
        plugin.private_user_aliases = {"openid-user": "canonical-user"}
        plugin.data = {
            "daily_plan": {},
            "users": {
                "canonical-user": {
                    "user_id": "canonical-user",
                    "nickname": "主要用户",
                    "enabled": True,
                }
            },
        }
        plugin._record_photo_reference_feedback_from_event = lambda _event: None
        plugin._stop_group_llm_reply_if_blocked = lambda *_args, **_kwargs: False
        plugin._sanitize_request_context_new_conversation_boundary = lambda *_args, **_kwargs: None
        plugin._repair_incomplete_tool_context_groups = lambda *_args, **_kwargs: None
        plugin._sanitize_private_companion_prompt_artifacts_in_request = lambda *_args, **_kwargs: None
        plugin._append_deepseek_tool_protocol_guard = lambda *_args, **_kwargs: None
        plugin._remember_external_llm_request_for_token_stats = lambda *_args, **_kwargs: None
        plugin._proactive_only_limited_passive_event = lambda *_args, **_kwargs: False
        plugin._proactive_only_blocks_passive_event = lambda *_args, **_kwargs: False
        plugin._trim_passive_request_context_if_needed = lambda *_args, **_kwargs: None
        plugin._start_passive_input_status_loop = lambda *_args, **_kwargs: None
        plugin._log_bookshelf_secret_skip = lambda *_args, **_kwargs: None
        plugin._is_target_private_user = lambda user_id, _user=None: user_id == "canonical-user"
        plugin._feature_enabled_or_temp_unlocked = (
            lambda feature, _default=False: feature == "inject_passive_states"
        )
        plugin._should_reply_during_rest = AsyncMock(return_value=(True, "disabled"))
        plugin._apply_busy_reply_gate_delay = AsyncMock(return_value=(0.0, "disabled"))
        plugin._enrich_request_context_image_placeholders = AsyncMock()
        plugin.apply_tts_enhancement_request = AsyncMock()
        plugin._append_forward_message_context_to_request = AsyncMock()
        plugin._append_non_target_private_identity_guard_to_request = AsyncMock()
        plugin._append_daily_review_guidance_to_request = AsyncMock()
        plugin._ensure_daily_state = AsyncMock(return_value=_period_state())
        plugin._record_request_prompt_fragment = AsyncMock()
        event = SimpleNamespace(
            unified_msg_origin="official:FriendMessage:openid-user",
            message_str="今晚想和你做点更私密的事",
            get_sender_id=lambda: "openid-user",
            is_private_chat=lambda: True,
        )
        request = SimpleNamespace(
            system_prompt="私聊人格",
            prompt=(
                "<!-- private_companion_state_v1 -->\n"
                "今晚想和你做点更私密的事"
            ),
            contexts=[],
            extra_user_content_parts=[],
        )

        class _StopAfterBoundary(Exception):
            pass

        append_boundary = plugin._append_private_active_period_boundary_to_request

        async def append_boundary_then_stop(*args, **kwargs):
            await append_boundary(*args, **kwargs)
            raise _StopAfterBoundary

        plugin._append_private_active_period_boundary_to_request = append_boundary_then_stop

        for _ in range(2):
            with self.assertRaises(_StopAfterBoundary):
                await plugin.inject_humanized_state(event, request)

        prompt = plugin._request_prompt_context_surface(request)
        self.assertEqual(prompt.count("【Bot 当前经期与互动边界】"), 1)
        self.assertEqual(prompt.count("自然、明确地拒绝或推迟这一次互动"), 1)
        self.assertEqual(request._private_companion_preferred_address, "主要用户")
        self.assertEqual(plugin._ensure_daily_state.await_count, 2)
        plugin._record_request_prompt_fragment.assert_awaited_once()

    def test_private_fingerprint_tracks_cycle_outside_first_three_conditions(self) -> None:
        plugin = _state_harness()
        neutral = {
            **_period_state(),
            "body_cycle": "不处于生理期",
            "conditions": [
                {"kind": "sleep", "label": "睡眠偏浅", "energy_delta": -1},
                {"kind": "dream", "label": "梦境余波", "mood": "恍惚"},
                {"kind": "hunger", "label": "有些饿", "energy_delta": -1},
            ],
        }
        period = {
            **neutral,
            "body_cycle": "想把动作放轻一些",
            "conditions": [
                *neutral["conditions"],
                {
                    "kind": "body_cycle",
                    "phase": "menstrual",
                    "label": "想把动作放轻一些",
                    "energy_delta": -10,
                },
            ],
        }

        neutral_fingerprint = plugin._private_passive_state_fingerprint(neutral, {})
        period_fingerprint = plugin._private_passive_state_fingerprint(period, {})
        snapshot = plugin._format_private_passive_state_snapshot(period, {})

        self.assertNotEqual(neutral_fingerprint, period_fingerprint)
        self.assertEqual(period_fingerprint["body_cycle_phase"], "menstrual")
        self.assertIn("周期状态：Bot 当前处于月经期阶段", snapshot)

    def test_private_period_boundary_survives_unchanged_delta_second_turn(self) -> None:
        plugin = _state_harness()
        plugin._passive_state_session_cache = {}
        state = _period_state()

        first_update = plugin._private_passive_state_update_for_prompt(
            session="default:FriendMessage:10001",
            state=state,
            current_user={},
            inbound_text="晚上好",
            lightweight=True,
        )
        second_update = plugin._private_passive_state_update_for_prompt(
            session="default:FriendMessage:10001",
            state=state,
            current_user={},
            inbound_text="今晚想和你做点更私密的事",
            lightweight=True,
        )
        first_surface = PromptSurface()
        second_surface = PromptSurface()
        plugin._add_private_active_period_boundary_to_surface(first_surface, state)
        plugin._add_private_active_period_boundary_to_surface(second_surface, state)

        self.assertTrue(first_update[0])
        self.assertEqual(second_update[0], "")
        self.assertIn("明确地拒绝或推迟", first_surface.render())
        self.assertIn("明确地拒绝或推迟", second_surface.render())
        second_fragment = second_surface.rendered_fragments()[0]
        self.assertEqual(second_fragment["key"], "state.period_boundary")
        self.assertEqual(second_fragment["priority"], 89)


if __name__ == "__main__":
    unittest.main()

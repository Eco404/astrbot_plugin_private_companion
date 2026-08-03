# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import logging
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from astrbot.api import logger as _astrbot_logger  # noqa: F401
except ModuleNotFoundError:
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_event_module = types.ModuleType("astrbot.api.event")
    astrbot_api_module.logger = logging.getLogger("qzone-life-publish-test")
    astrbot_event_module.AstrMessageEvent = object
    astrbot_module.api = astrbot_api_module
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)
    sys.modules.setdefault("astrbot.api.event", astrbot_event_module)

from astrbot_plugin_private_companion.qzone_integration import QzoneMixin


class _PlanHarness(QzoneMixin):
    qzone_life_publish_max_daily = 3
    qzone_life_publish_probability = 1.0
    qzone_life_publish_window_mode = "custom"
    qzone_life_publish_windows = "07:00-11:00\n12:00-13:00\n18:00-22:00"
    qzone_life_publish_allow_insomnia_night = False
    qzone_life_publish_intra_day_gap_minutes = 60
    qzone_life_publish_min_interval_hours = 0

    def __init__(self) -> None:
        self.data = {"daily_plan": {"date": time.strftime("%Y-%m-%d"), "items": []}}

    def _has_active_insomnia_state(self) -> bool:
        return False


class _CommentHarness(QzoneMixin):
    qzone_comment_inbox_recent_posts = 5

    def __init__(self, post, event_id: str = "100") -> None:
        self.post = post
        self.event_id = event_id
        self.data = {"qzone_integration": {}}
        self.sent = []

    def _qzone_available(self, _event=None) -> bool:
        return True

    async def _qzone_get_cookies(self, _event=None) -> str:
        return "uin=o123; skey=x"

    def _qzone_context_from_cookies(self, _cookies: str) -> dict:
        return {"uin": 123}

    async def _qzone_query_feeds(self, *_args, **_kwargs):
        return [self.post]

    async def _qzone_decide_comment_reply(self, _post, _comment, **_kwargs) -> dict:
        return {"decision": "reply", "reply": "收到啦", "reason": "ok"}

    async def _qzone_reply_to_comment(self, _event, _post, _comment, reply: str) -> str:
        self.sent.append(reply)
        return reply

    def _save_data_sync(self) -> None:
        pass


class _Event:
    def __init__(self, sender_id: str) -> None:
        self.sender_id = sender_id

    def get_sender_id(self) -> str:
        return self.sender_id


class QzoneLifePublishPlanTests(unittest.IsolatedAsyncioTestCase):
    def test_nested_failure_code_wins_over_empty_normalized_code(self) -> None:
        self.assertEqual(_PlanHarness._qzone_response_code({"code": None, "_raw_code": -3000}), -3000)
        self.assertEqual(_PlanHarness._qzone_response_code({"code": "", "ret": -1}), -1)
        self.assertEqual(_PlanHarness._qzone_response_code({"code": 0, "_raw_code": 0}), 0)

    def test_windows_are_unlimited_and_overlaps_merge(self) -> None:
        windows = _PlanHarness._qzone_parse_windows("07:00-09:00\n08:00-11:00\n12:00-13:00\n18:00-19:00\n20:00-21:00")
        self.assertEqual(windows, [(420, 660), (720, 780), (1080, 1140), (1200, 1260)])

    def test_n_one_always_plans_one_item(self) -> None:
        harness = _PlanHarness()
        harness.qzone_life_publish_max_daily = 1
        local = time.localtime()
        now = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 8, 0, 0, 0, 0, -1))
        with patch("astrbot_plugin_private_companion.qzone_integration.random.random", return_value=0.0):
            plan = harness._qzone_life_publish_daily_plan({}, now=now)
        self.assertEqual(plan["target_count"], 1)
        self.assertEqual(len(plan["items"]), 1)

    def test_plan_is_reused_for_the_same_day(self) -> None:
        harness = _PlanHarness()
        state = {}
        with patch("astrbot_plugin_private_companion.qzone_integration.random.random", return_value=0.0):
            first = harness._qzone_life_publish_daily_plan(state, now=time.time())
            second = harness._qzone_life_publish_daily_plan(state, now=time.time() + 60)
        self.assertIs(first, second)

    async def test_immediate_comment_reply_marks_stable_key_once(self) -> None:
        comment = SimpleNamespace(comment_id="c1", uin="100", name="user", content="我刚刚评论啦", raw={})
        post = SimpleNamespace(tid="post1", comments=[comment])
        harness = _CommentHarness(post)

        result = await harness._qzone_reply_my_comment(_Event("100"), comment_hint="刚刚评论")

        self.assertEqual(result["status"], "replied")
        self.assertEqual(harness.sent, ["收到啦"])
        state = harness.data["qzone_integration"]
        self.assertIn("c1", state["comment_inbox_replied_ids"])
        self.assertEqual(len(state["comment_inbox_replied_keys"]), 1)

        again = await harness._qzone_reply_my_comment(_Event("100"), comment_hint="刚刚评论")
        self.assertEqual(again["status"], "not_found")

    async def test_immediate_comment_reply_refuses_ambiguous_matches(self) -> None:
        comments = [
            SimpleNamespace(comment_id="c1", uin="100", name="user", content="今天真不错", raw={}),
            SimpleNamespace(comment_id="c2", uin="200", name="other", content="今天真不错", raw={}),
        ]
        harness = _CommentHarness(SimpleNamespace(tid="post1", comments=comments))

        result = await harness._qzone_reply_my_comment(_Event("0"), comment_hint="今天真不错")

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(harness.sent, [])

    async def test_known_sender_wins_over_foreign_exact_hint(self) -> None:
        comments = [
            SimpleNamespace(comment_id="c1", uin="100", name="user", content="这是我刚刚留的评论", raw={}),
            SimpleNamespace(comment_id="c2", uin="200", name="other", content="精确关键词", raw={}),
        ]
        harness = _CommentHarness(SimpleNamespace(tid="post1", comments=comments))

        result = await harness._qzone_reply_my_comment(_Event("100"), comment_hint="精确关键词")

        self.assertEqual(result["status"], "replied")
        self.assertEqual(result["comment"], "这是我刚刚留的评论")

    async def test_immediate_comment_reply_failure_is_retryable(self) -> None:
        comment = SimpleNamespace(comment_id="c1", uin="100", name="user", content="我刚刚评论啦", raw={})
        harness = _CommentHarness(SimpleNamespace(tid="post1", comments=[comment]))

        async def fail_reply(*_args, **_kwargs):
            raise RuntimeError("评论失败 code=-3000")

        harness._qzone_reply_to_comment = fail_reply
        result = await harness._qzone_reply_my_comment(_Event("100"), comment_hint="刚刚评论")

        self.assertEqual(result["status"], "error")
        state = harness.data["qzone_integration"]
        self.assertIn("c1", state["comment_inbox_retry_ids"])
        self.assertNotIn("c1", state.get("comment_inbox_replied_ids", []))

    async def test_unknown_delivery_result_is_not_retried(self) -> None:
        comment = SimpleNamespace(comment_id="c1", uin="100", name="user", content="我刚刚评论啦", raw={})
        harness = _CommentHarness(SimpleNamespace(tid="post1", comments=[comment]))

        async def fail_reply(*_args, **_kwargs):
            raise TimeoutError("connection timed out")

        harness._qzone_reply_to_comment = fail_reply
        result = await harness._qzone_reply_my_comment(_Event("100"), comment_hint="刚刚评论")

        self.assertEqual(result["status"], "error")
        self.assertFalse(result["retryable"])
        state = harness.data["qzone_integration"]
        self.assertEqual(state["last_comment_inbox_status"], "tool_delivery_unknown")
        self.assertNotIn("c1", state.get("comment_inbox_retry_ids", []))


if __name__ == "__main__":
    unittest.main()

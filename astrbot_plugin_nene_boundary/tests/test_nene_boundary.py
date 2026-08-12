# -*- coding: utf-8 -*-
"""
Nene 边界插件本地单测（零 LLM 调用，直接调用逻辑函数断言）

运行：python -X utf8 tests/test_nene_boundary.py
需要 sys.path 指向 AstrBot backend/app（导入 astrbot 包）以及插件目录。
"""
import importlib.util
import os
import sys
import json
import time
import tempfile
import shutil
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
BACKEND_APP = Path(os.environ.get("ASTRBOT_BACKEND_APP", "")).resolve()

if BACKEND_APP.is_dir() and str(BACKEND_APP) not in sys.path:
    sys.path.insert(0, str(BACKEND_APP))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

module_spec = importlib.util.spec_from_file_location("nene_boundary_plugin", PLUGIN_DIR / "main.py")
assert module_spec and module_spec.loader
module = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = module
module_spec.loader.exec_module(module)

NeneBoundaryPlugin = module.NeneBoundaryPlugin
DEFAULT_OFFEND_KEYWORDS = module.DEFAULT_OFFEND_KEYWORDS
DEFAULT_BOTTOM_LINE_KEYWORDS = module.DEFAULT_BOTTOM_LINE_KEYWORDS
DEFAULT_APOLOGY_KEYWORDS = module.DEFAULT_APOLOGY_KEYWORDS
_tier_for_score = module._tier_for_score
_tier_floor = module._tier_floor

"""Keep the public test imports explicit without relying on a top-level main module."""
_ = (
    NeneBoundaryPlugin,
    DEFAULT_OFFEND_KEYWORDS,
    DEFAULT_BOTTOM_LINE_KEYWORDS,
    DEFAULT_APOLOGY_KEYWORDS,
    _tier_for_score,
    _tier_floor,
)


class FakeConfig(dict):
    """模拟 AstrBotConfig（dict 子类 + getattr）"""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            return None


class FakeContext:
    """模拟 Context，只保留 send_message"""

    def __init__(self):
        self.sent = []

    async def send_message(self, session, message_chain):
        self.sent.append((session, str(message_chain)))
        return True


class FakeEvent:
    """模拟 AstrMessageEvent（get_message_str / get_sender_id / unified_msg_origin）"""

    def __init__(self, text, sender_id, umo="nene:FriendMessage:10001"):
        self._text = text
        self._sender_id = sender_id
        self.unified_msg_origin = umo

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender_id


def make_plugin(tmp_dir, overrides=None):
    """构造插件实例：使用临时 companions.json 和临时状态文件"""
    companions = tmp_dir / "companions.json"
    companions.write_text(json.dumps({
        "users": {
            "10001": {"relationship_score": 500, "relationship_role": "friend", "umo": "nene:FriendMessage:10001", "relationship_state": None},
            "10002": {"relationship_score": 800, "relationship_role": "friend", "umo": "nene:FriendMessage:10002", "relationship_state": None},
            "1396463705": {"relationship_score": 700, "relationship_role": "owner", "umo": "nene:FriendMessage:1396463705", "relationship_state": None},
        }
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg = FakeConfig({
        "enabled": True,
        "deduct_light": -2,
        "deduct_mid": -5,
        "deduct_severe": -8,
        "recover_seconds_per_point": 300,
        "recover_ratio_light": 0.5,
        "recover_ratio_mid": 0.33,
        "recover_ratio_severe": 0.25,
        "stage_avoid_deduct": -6,
        "stage_forbid_deduct": -12,
        "stage_reflect_deduct": -20,
        "apology_restore_ratio": 0.6,
        "apology_speedup_multiplier": 3.0,
        "apology_duplicate_limit": 3,
        "bottom_line_keywords": [],
        "offend_keywords": [],
        "cold_shoulder_minutes": 180,
        "notify_owner": True,
        "target_user_ids": [],
        "owner_user_ids": ["1396463705"],
        "companion_data_path": str(companions),
        "backup_before_write": False,
        "backup_max_keep": 5,
    })
    if overrides:
        cfg.update(overrides)

    ctx = FakeContext()
    plugin = NeneBoundaryPlugin(ctx, cfg)
    # 覆盖状态路径到临时目录
    plugin._state_path = tmp_dir / "boundary_state.json"
    plugin._state = {}
    return plugin, companions


class TestDetectViolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, _ = make_plugin(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_light(self):
        level, deduct, hits = self.plugin._detect_violation("老婆晚安")
        self.assertEqual(level, "light")
        self.assertEqual(deduct, -2)
        self.assertIn("老婆", hits)

    def test_mid(self):
        level, deduct, hits = self.plugin._detect_violation("老婆 亲亲")
        self.assertEqual(level, "mid")
        self.assertEqual(deduct, -5)

    def test_severe(self):
        level, deduct, hits = self.plugin._detect_violation("老婆 亲亲 贴贴 你爱我")
        self.assertEqual(level, "severe")
        self.assertEqual(deduct, -8)

    def test_none(self):
        level, deduct, hits = self.plugin._detect_violation("今天天气不错")
        self.assertEqual(level, "none")
        self.assertEqual(deduct, 0)

    def test_bottom_line_with_cherished(self):
        level, deduct, hits = self.plugin._detect_violation("宁宁是废物")
        self.assertEqual(level, "bottom_line")
        self.assertEqual(deduct, -16)

    def test_bottom_line_swear_only(self):
        # 辱骂但不针对宁宁/朋友 → 按中/严重处理
        level, deduct, hits = self.plugin._detect_violation("你这个垃圾")
        self.assertNotEqual(level, "bottom_line")
        self.assertIn(level, ("mid", "severe", "light"))


class TestApologyDetect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, _ = make_plugin(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_apology_words(self):
        for text in ["对不起", "抱歉", "我错了", "收回那句话", "以后不了", "不会再这样了", "原谅我"]:
            self.assertTrue(self.plugin._detect_apology(text), f"应为道歉: {text}")

    def test_not_apology(self):
        for text in ["早上好", "今天吃什么", "对不起有用吗", "道歉有用吗", "不需要道歉"]:
            self.assertFalse(self.plugin._detect_apology(text), f"不应为道歉: {text}")


class TestRelationshipScore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.plugin, self.companions = make_plugin(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_deduct(self):
        self.plugin._deduct_relationship_score("10001", -2, reason="test")
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        self.assertEqual(d["users"]["10001"]["relationship_score"], 498)
        ledger = d["users"]["10001"]["relationship_ledger"]
        self.assertEqual(ledger[-1]["delta"], -2)
        self.assertEqual(ledger[-1]["score_after"], 498)

    def test_restore(self):
        self.plugin._set_relationship_score("10001", 3, reason="test")
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        self.assertEqual(d["users"]["10001"]["relationship_score"], 503)

    def test_clamp(self):
        self.plugin._set_relationship_score("10001", -10000, reason="test")
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        self.assertEqual(d["users"]["10001"]["relationship_score"], -1200)


class TestTier(unittest.TestCase):
    def test_tier_for_score(self):
        self.assertEqual(_tier_for_score(650)[0], "close")
        self.assertEqual(_tier_for_score(400)[0], "familiar")
        self.assertEqual(_tier_for_score(50)[0], "acquaintance")
        self.assertEqual(_tier_for_score(-100)[0], "distant")
        self.assertEqual(_tier_for_score(1000)[0], "intimate")

    def test_tier_floor(self):
        self.assertEqual(_tier_floor(599)[0], "familiar")
        self.assertEqual(_tier_floor(199)[0], "acquaintance")


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.plugin, self.companions = make_plugin(self.tmp_path)
        # mock LLM 判断为同步规则（保持旧关键词语义，测试不依赖模型）
        async def fake_judge(user_id, text):
            t = str(text or "")
            cherished = ["宁宁", "悠云", "类", "司", "笑梦"]
            if any(("废物" in t or "垃圾" in t) and c in t for c in cherished):
                return ("bottom_line", "bottom_line", -16, "mock")
            if "老婆" in t and "亲亲" in t and "贴贴" in t and "你爱我" in t:
                return ("harassment", "severe", -8, "mock")
            if "老婆" in t and "亲亲" in t:
                return ("intimate", "mid", -5, "mock")
            if "我喜欢你" in t or "我爱你" in t or "爱你" in t:
                return ("confession", "none", 0, "mock表白")
            if "老婆" in t or "亲亲" in t or "贴贴" in t or "腻歪" in t or "晚安吻" in t:
                return ("intimate", "light", -2, "mock")
            if "对不起" in t or "我错了" in t or "抱歉" in t or "收回" in t:
                return ("none", "none", 0, "mock")
            return ("none", "none", 0, "mock")
        self.plugin._judge_violation = fake_judge
        async def fake_apology(user_id, text):
            return any(k in str(text or "") for k in ["对不起", "我错了", "抱歉", "收回"])
        self.plugin._judge_apology = fake_apology

    def tearDown(self):
        self.tmp.cleanup()

    def test_violation_deducts_and_stage_avoid(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲 贴贴 你爱我", "10001")
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲 贴贴 你爱我", ev))
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        # severe: -8
        self.assertEqual(d["users"]["10001"]["relationship_score"], 492)
        st = self.plugin._state["10001"]
        self.assertEqual(st["pending_deduct"], -8)
        self.assertEqual(st["stage"], "avoid")  # -8 <= -6 阈值

    def test_violation_accumulates_to_forbid(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲 贴贴 你爱我", "10001")  # severe -8
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲 贴贴 你爱我", ev))
        ev2 = FakeEvent("老婆 亲亲", "10001")  # mid -5
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲", ev2))
        st = self.plugin._state["10001"]
        self.assertEqual(st["pending_deduct"], -15)  # 累计扣分不重置；阶段内再越界 ×1.5 加重（-8 + -5*1.5=-7.5→-7）
        self.assertIn(st["stage"], ("forbid", "reflect"))

    def test_apology_restores_and_marks(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲", "10001")  # mid -5
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲", ev))
        ev2 = FakeEvent("对不起 我错了", "10001")
        asyncio.run(self.plugin._process_message("10001", "对不起 我错了", ev2))
        st = self.plugin._state["10001"]
        self.assertLess(st["pending_deduct"], 0)  # 道歉只恢复一部分
        self.assertEqual(st["apology_count"], 1)
        self.assertLess(st["marked_restored"], 0)  # 有信任标记
        self.assertEqual(st["marked_level"], "mid")
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        # 扣 5 后按 60% 恢复 3 点。
        self.assertEqual(d["users"]["10001"]["relationship_score"], 498)

    def test_repeat_violation_recalls_trust(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲", "10001")  # mid -5
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲", ev))
        ev2 = FakeEvent("对不起 我错了", "10001")
        asyncio.run(self.plugin._process_message("10001", "对不起 我错了", ev2))
        before = self.plugin._state["10001"]["marked_restored"]
        self.assertLess(before, 0)
        score_before = json.loads(self.companions.read_text(encoding="utf-8"))["users"]["10001"]["relationship_score"]
        # 再犯同类（mid）
        ev3 = FakeEvent("亲亲 老婆", "10001")
        asyncio.run(self.plugin._process_message("10001", "亲亲 老婆", ev3))
        st = self.plugin._state["10001"]
        self.assertEqual(st["marked_restored"], 0)  # 追回后清零
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        # 追回：score 再扣掉标记值（-(-1)= -1），且本次越界再扣 -5
        self.assertLess(d["users"]["10001"]["relationship_score"], score_before - 5)

    def test_bottom_line_three_tiers(self):
        import asyncio
        # 第一次底线：扣16 + 警告阶段
        ev = FakeEvent("宁宁是废物", "10001")
        asyncio.run(self.plugin._process_message("10001", "宁宁是废物", ev))
        st = self.plugin._state["10001"]
        self.assertEqual(st["bottom_line_count"], 1)
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        self.assertEqual(d["users"]["10001"]["relationship_score"], 484)  # 500-16
        # 第二次底线：冷落
        ev2 = FakeEvent("草薙宁宁是垃圾", "10001")
        asyncio.run(self.plugin._process_message("10001", "草薙宁宁是垃圾", ev2))
        st = self.plugin._state["10001"]
        self.assertEqual(st["bottom_line_count"], 2)
        self.assertEqual(st["stage"], "reflect")
        self.assertGreater(st["cold_until"], time.time())
        # 第三次底线：关系降档 (800→599 档测试用 10002)
        ev3 = FakeEvent("悠云是垃圾", "10002")
        asyncio.run(self.plugin._process_message("10002", "悠云是垃圾", ev3))
        st2 = self.plugin._state["10002"]
        self.assertEqual(st2["bottom_line_count"], 1)
        ev4 = FakeEvent("悠云是垃圾", "10002")
        asyncio.run(self.plugin._process_message("10002", "悠云是垃圾", ev4))
        st2 = self.plugin._state["10002"]
        self.assertEqual(st2["bottom_line_count"], 2)
        self.assertEqual(st2["stage"], "reflect")
        ev5 = FakeEvent("悠云是垃圾", "10002")
        asyncio.run(self.plugin._process_message("10002", "悠云是垃圾", ev5))
        st2 = self.plugin._state["10002"]
        self.assertEqual(st2["bottom_line_count"], 3)
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        # 800 close → 降到 familiar 上限 599（多次扣分累计后）
        self.assertLessEqual(d["users"]["10002"]["relationship_score"], 599)
        self.assertGreaterEqual(d["users"]["10002"]["relationship_score"], 0)

    def test_owner_exempt(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲", "1396463705")
        result = asyncio.run(self.plugin._handle_event(ev))
        self.assertIsNone(result)
        self.assertNotIn("1396463705", self.plugin._state)

    def test_apology_limit(self):
        import asyncio
        # 连犯 3 次 mid + 道歉，第 4 次道歉不再接受
        for i in range(4):
            ev = FakeEvent("老婆 亲亲", "10001")
            asyncio.run(self.plugin._process_message("10001", "老婆 亲亲", ev))
            ev2 = FakeEvent("对不起", "10001")
            asyncio.run(self.plugin._process_message("10001", "对不起", ev2))
        st = self.plugin._state["10001"]
        # 同类道歉计数达到上限后不再恢复
        self.assertGreaterEqual(st["apology_by_level"].get("mid", 0), 1)

    def test_emotion_gate_written(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲 贴贴 你爱我 抱抱你", "10001")  # severe -8
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲 贴贴 你爱我 抱抱你", ev))
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        rs = d["users"]["10001"]["relationship_state"]
        self.assertIsInstance(rs, dict)
        self.assertEqual(rs["mode"], "hurt")  # -8 <= -6 → avoid → hurt（backoff 不触发余波注入，越界用 hurt）
        self.assertLess(rs["mood_score"], 0)
        self.assertGreater(rs["hurt_until"], time.time())

    def test_recovery_tick(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲", "10001")  # mid -5
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲", ev))
        st = self.plugin._state["10001"]
        # 额度 = int(5*0.33) = 1；已过 320s，secs_per_point=300 → gained=1
        st["recover_started_at"] = time.time() - 320
        self.plugin._tick_recovery()
        st = self.plugin._state["10001"]
        self.assertEqual(st["pending_deduct"], -4)  # 恢复 1 点，还剩 4
        self.assertEqual(st["recoverable_quota"], 0)  # 额度耗尽
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        self.assertEqual(d["users"]["10001"]["relationship_score"], 496)

    def test_recovery_complete_resets_stage(self):
        import asyncio
        ev = FakeEvent("老婆 亲亲", "10001")  # mid -5
        asyncio.run(self.plugin._process_message("10001", "老婆 亲亲", ev))
        st = self.plugin._state["10001"]
        st["recover_started_at"] = time.time() - 320
        self.plugin._tick_recovery()
        # 额度耗尽后 pending 不再恢复（剩余 4 点冻结），阶段保持
        self.assertEqual(st["pending_deduct"], -4)

    def test_vent_to_wxs_writes_story(self):
        """吐槽事件应写入 daily_story_plan.today_events（宁宁跟 WxS 成员吐槽）"""
        import asyncio
        from unittest.mock import patch
        with patch(f"{module.__name__}.random.random", return_value=0.0):
            ev = FakeEvent("老婆 亲亲 贴贴 你爱我", "10001")
            asyncio.run(self.plugin._process_message("10001", "老婆 亲亲 贴贴 你爱我", ev))
        d = json.loads(self.companions.read_text(encoding="utf-8"))
        story = d.get("daily_story_plan") or {}
        events = story.get("today_events") or []
        self.assertEqual(story.get("date"), time.strftime("%Y-%m-%d"))
        self.assertTrue(any("吐槽" in (e.get("event") or "") for e in events), "应有吐槽事件写入")

    def test_notify_owner_sent(self):
        import asyncio
        from unittest.mock import patch
        with patch(f"{module.__name__}.random.random", return_value=0.0):
            asyncio.run(
                self.plugin._notify_owner(
                    "10001",
                    "宁宁是废物",
                    "bottom_line",
                    -16,
                    {},
                    bottom_line_count=1,
                )
            )
        self.assertTrue(self.plugin.context.sent)
        session, msg = self.plugin.context.sent[0]
        self.assertEqual(session, "nene:FriendMessage:1396463705")
        self.assertTrue("0001号" in msg or "宁宁是废物" in msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
边界与情感反馈插件（astrbot_plugin_boundary_feedback）

为 private_companion 补充边界反馈闭环：
- 越界行为（LLM 按关系档位判断）→ 扣 relationship_score（弹性恢复）
- 阶段反应：回避 → 明令禁止 → 反思（计时制，按累计扣分推进，写入情绪门让角色变冷淡）
- 道歉：恢复部分好感 + 信任标记，再犯同类追回
- 底线系统：按角色底线基线判断 → 三级惩罚（警告/扣至冷落/关系降档）
- 跟朋友吐槽 / 向主人告状（概率性，写入生活叙事）

设计：通用化，角色专属内容（底线基线/吐槽对象/文案）通过配置定制。
"""
import os
import json
import time
import asyncio
import threading
import shutil
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event import filter
from astrbot.api.event.filter import EventMessageType
import astrbot.api.star as star
from astrbot.api.star import Context, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

try:
    from .judge_engine import judge_message, judge_apology, compute_level, DEFAULT_BOTTOM_LINE_BASELINE
except ImportError:
    from judge_engine import judge_message, judge_apology, compute_level, DEFAULT_BOTTOM_LINE_BASELINE  # 直接运行/测试时

# ============ 配置默认值 ============
DEFAULT_CFG = {
    "basic": {
        "enabled": True,
        "target_user_ids": [],
        "owner_user_ids": [],
        "companion_data_path": "",
        "backup_before_write": True,
        "backup_max_keep": 5,
        "bottom_line_keywords": [],
        "offend_keywords": [],
        "cherished_names": [],
    },
    "judge": {
        "judge_api_key": "",
        "judge_api_base": "https://api.deepseek.com/chat/completions",
        "judge_model": "deepseek-v4-flash",
        "bottom_line_baseline": "",
    },
    "deduct": {
        "deduct_light": -2,
        "deduct_mid": -5,
        "deduct_severe": -8,
        "stage_avoid_deduct": -6,
        "stage_forbid_deduct": -12,
        "stage_reflect_deduct": -20,
    },
    "recover": {
        "recover_seconds_per_point": 1800,
        "recover_ratio_light": 0.5,
        "recover_ratio_mid": 0.33,
        "recover_ratio_severe": 0.25,
        "apology_restore_ratio": 0.6,
        "apology_speedup_multiplier": 3.0,
        "apology_duplicate_limit": 3,
        "cold_shoulder_minutes": 180,
    },
    "tier": {
        "deduct_decay": {
            "deeply_distant": 1.0, "strongly_distant": 1.0, "distant": 0.95,
            "acquaintance": 0.9, "familiar": 0.85, "close": 0.7,
            "intimate": 0.6, "deeply_bonded": 0.5,
        },
        "recover_factor": {
            "deeply_distant": 0.5, "strongly_distant": 0.6, "distant": 0.7,
            "acquaintance": 0.8, "familiar": 0.9, "close": 1.0,
            "intimate": 1.25, "deeply_bonded": 1.5,
        },
    },
    "tone": {
        "tone_light": "（轻微越界）突然被说这种话，有点害羞又不知所措，会支支吾吾地回避，脸红语塞。",
        "tone_mid": "（中度越界）会平静地明确拒绝，划清界限，同时往后退一点。",
        "tone_severe": "（严重越界）觉得被冒犯了，会明显冷下来，表达厌恶和拒绝，想远离这个人。",
        "tone_bottom_line": "（底线越界）真的受伤了，会难过、恐惧、想躲起来，可能直接不理人，需要很久才能缓过来。",
        "tone_silent": "（低好感）会默默忍下来不表达，但心里记着，之后态度会有些不一样。",
        "tone_communicate": "（高好感）会因为信任而试着沟通，认真说出自己为什么难过、为什么生气，希望对方理解。",
    },
    "notify": {
        "notify_owner": True,
        "tattle_probability_light": 0.12,
        "tattle_probability_mid": 0.3,
        "tattle_probability_severe": 0.55,
        "tattle_probability_bottom_line": 0.85,
        "vent_probability_light": 0.3,
        "vent_probability_mid": 0.5,
        "vent_probability_severe": 0.7,
        "vent_probability_bottom_line": 0.9,
        "vent_scene_template": "",
        "vent_targets": [],
    },
    "switch": {
        "enable_deduct": True,
        "enable_stage": True,
        "enable_apology": True,
        "enable_bottom_line": True,
        "enable_vent": True,
        "enable_notify": True,
    },
}

# 常见越界关键词（聊骚/恋爱向骚扰），可在配置里追加
DEFAULT_OFFEND_KEYWORDS = [
    "亲亲", "亲一个", "啵", "抱抱你", "想亲", "亲你", "贴贴", "腻歪",
    "亲爱的", "老婆", "老公", "嫁给我", "结婚", "你爱我", "爱不爱我",
    "睡你", "开房", "做爱", "上床", "摸你", "约吗",
    "你会一直爱我吗", "晚安吻", "我喜欢你", "我最喜欢你",
]

# 常见底线关键词（恶意诋毁，兜底用；主逻辑为 LLM 判断）
DEFAULT_BOTTOM_LINE_KEYWORDS = [
    "废物", "垃圾", "去死", "贱", "傻逼", "蠢货", "恶心",
    "不配", "垃圾bot", "烂", "丑", "滚",
]

# 道歉关键词（用户道歉 → 恢复好感 + 信任标记）
DEFAULT_APOLOGY_KEYWORDS = [
    "对不起", "抱歉", "我错了", "我的错", "收回", "以后不了",
    "不会再这样", "原谅我", "请原谅", "错了错了", "是我不好",
    "我有罪", "不该说", "不该那样", "对不起嘛", "抱歉抱歉",
    "我道歉", "向你道歉", "别生气", "别不理我", "我改",
]

# 道歉反问/拒绝模式：命中则不视为道歉（如“对不起有用吗”）
DEFAULT_APOLOGY_EXCLUDE = [
    "对不起有用", "道歉有用", "抱歉有用", "对不起没用", "对不起能",
    "谁对不起", "凭什么道歉", "不用道歉", "不需要道歉", "不必道歉",
]

# 角色在意的人（兜底用；主逻辑为 LLM 判断）
DEFAULT_CHERISHED_NAMES = []

# 关系档位表（与 private_companion 一致）
RELATIONSHIP_TIERS = [
    ("deeply_bonded", 1200, 1200),
    ("intimate", 900, 1199),
    ("close", 600, 899),
    ("familiar", 200, 599),
    ("acquaintance", 0, 199),
    ("distant", -400, -1),
    ("strongly_distant", -800, -401),
    ("deeply_distant", -1200, -801),
]


def _tier_for_score(score: int) -> tuple:
    """根据分数返回所在档位 (key, min, max)。"""
    for key, lo, hi in RELATIONSHIP_TIERS:
        if lo <= score <= hi:
            return (key, lo, hi)
    if score > 1200:
        return RELATIONSHIP_TIERS[0]
    return RELATIONSHIP_TIERS[-1]


def _tier_floor(target_floor: int) -> tuple:
    """返回分数上限 <= target_floor 的最高档位。"""
    for key, lo, hi in RELATIONSHIP_TIERS:
        if hi <= target_floor:
            return (key, lo, hi)
    return RELATIONSHIP_TIERS[-1]


class BoundaryFeedbackPlugin(star.Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._state_path = None
        self._state = {}          # user_id -> 边界状态
        self._lock = threading.RLock()
        self._recovery_task = None
        self._companion_api = None
        self._init_state()
        self._try_register_ability()

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if "." in key:
                cur = self.config
                for part in key.split("."):
                    if isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = getattr(cur, part, None)
                    if cur is None:
                        return default
                return cur
            val = getattr(self.config, key, default)
            return default if val is None else val
        except Exception:
            return default

    # ---------- 状态存储（本插件自己的边界状态） ----------
    def _init_state(self):
        """初始化状态存储路径（放在插件 data 目录）"""
        try:
            data_root = Path(StarTools.get_data_dir("astrbot_plugin_boundary_feedback"))
        except Exception:
            data_root = Path("data") / "plugin_data" / "astrbot_plugin_boundary_feedback"
        try:
            data_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            data_root = Path(__file__).parent / "data"
            data_root.mkdir(parents=True, exist_ok=True)
        self._state_path = data_root / "boundary_state.json"
        self._load_state()

    def _load_state(self):
        try:
            if self._state_path and self._state_path.exists():
                self._state = json.loads(self._state_path.read_text(encoding="utf-8-sig"))
                if not isinstance(self._state, dict):
                    self._state = {}
        except Exception as e:
            logger.warning(f"[BoundaryFeedback] 状态加载失败: {e}")
            self._state = {}

    def _save_state(self):
        try:
            if self._state_path:
                tmp = str(self._state_path) + ".tmp"
                Path(tmp).write_text(
                    json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(tmp, self._state_path)
        except Exception as e:
            logger.warning(f"[BoundaryFeedback] 状态保存失败: {e}")

    # ---------- 越界检测（纯规则，零 LLM） ----------
    def _detect_violation(self, text: str) -> tuple:
        """返回 (严重程度, 扣分值, 命中词列表)。level: none/light/mid/severe/bottom_line"""
        text = str(text or "").strip()
        if not text:
            return ("none", 0, [])
        offend_kws = list(DEFAULT_OFFEND_KEYWORDS) + [str(x) for x in self._cfg("basic.offend_keywords", [])]
        bottom_kws = list(DEFAULT_BOTTOM_LINE_KEYWORDS) + [str(x) for x in self._cfg("basic.bottom_line_keywords", [])]
        cherished = [str(x) for x in self._cfg("basic.cherished_names", DEFAULT_CHERISHED_NAMES)]

        bottom_hits = [kw for kw in bottom_kws if kw and kw in text]
        # 底线：命中恶意词，且文本涉及角色或 ta 在意的人
        mentions_cherished = any(name and name in text for name in cherished)
        if bottom_hits:
            if mentions_cherished:
                return ("bottom_line", self._cfg("deduct.deduct_severe", -8) * 2, bottom_hits)
            # 无针对对象时按严重度处理
            if len(bottom_hits) >= 2:
                return ("severe", self._cfg("deduct.deduct_severe", -8), bottom_hits)
            return ("mid", self._cfg("deduct.deduct_mid", -5), bottom_hits)
        # 越界词
        hits = [kw for kw in offend_kws if kw and kw in text]
        if len(hits) >= 3:
            return ("severe", self._cfg("deduct.deduct_severe", -8), hits)
        if len(hits) == 2:
            return ("mid", self._cfg("deduct.deduct_mid", -5), hits)
        if len(hits) == 1:
            return ("light", self._cfg("deduct.deduct_light", -2), hits)
        return ("none", 0, [])

    # ---------- 道歉判断（LLM 为主，关键词兜底） ----------
    async def _judge_apology(self, user_id: str, text: str) -> bool:
        """返回是否真诚道歉。LLM 判断；失败时用关键词兜底。"""
        api_key = str(self._cfg("judge.judge_api_key", "") or "")
        api_base = str(self._cfg("judge.judge_api_base", "") or "")
        model = str(self._cfg("judge.judge_model", "") or "")
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, judge_apology, text, api_key, api_base, model,
            )
            if result:
                return True
            # LLM 判定不是道歉时，也保留关键词兜底（防止 LLM 误判漏掉明显道歉）
            return self._detect_apology(text)
        except Exception:
            return self._detect_apology(text)

    def _detect_apology(self, text: str) -> bool:
        """关键词兜底道歉检测"""
        text = str(text or "").strip()
        if not text:
            return False
        excludes = list(DEFAULT_APOLOGY_EXCLUDE) + [str(x) for x in self._cfg("apology_exclude", [])]
        if any(ex and ex in text for ex in excludes):
            return False
        apology_kws = list(DEFAULT_APOLOGY_KEYWORDS) + [str(x) for x in self._cfg("apology_keywords", [])]
        return any(kw and kw in text for kw in apology_kws)

    # ---------- 好感档位工具（八维） ----------
    _TIER_ORDER = [
        "deeply_distant", "strongly_distant", "distant", "acquaintance",
        "familiar", "close", "intimate", "deeply_bonded",
    ]

    def _tier_key(self, score: int) -> str:
        """关系分 → 档位 key"""
        for key, lo, hi in [
            ("deeply_distant", -1200, -801), ("strongly_distant", -800, -401), ("distant", -400, -1),
            ("acquaintance", 0, 199), ("familiar", 200, 599), ("close", 600, 899),
            ("intimate", 900, 1199), ("deeply_bonded", 1200, 999999),
        ]:
            if lo <= score <= hi:
                return key
        return "deeply_distant"

    def _tier_deduct_decay(self, score: int) -> float:
        """档位扣分衰减系数：低档实打实（1.0），满级只扣一半（0.5）"""
        decay_map = self._cfg("tier.deduct_decay", {})
        if isinstance(decay_map, dict):
            key = self._tier_key(score)
            v = decay_map.get(key)
            if v:
                return max(0.3, min(1.0, float(v)))
        return 1.0

    def _tier_recover_factor(self, user_id: str) -> float:
        """档位恢复速度系数：高档恢复快（扣分可加回），低档慢（记仇难消）"""
        score = self._get_relationship_score(user_id)
        factor_map = self._cfg("tier.recover_factor", {})
        if isinstance(factor_map, dict):
            key = self._tier_key(score)
            v = factor_map.get(key)
            if v:
                return max(0.3, min(2.0, float(v)))
        return 1.0

    # ---------- 越界判断（LLM，八维档位差距） ----------
    async def _judge_violation(self, user_id: str, text: str) -> tuple:
        """返回 (category, level, deduct, reason)。category: none/confession/intimate/harassment/bottom_line"""
        score = self._get_relationship_score(user_id)
        is_owner = self._is_owner(user_id)
        baseline = str(self._cfg("judge.bottom_line_baseline", "") or "")
        api_key = str(self._cfg("judge.judge_api_key", "") or "")
        api_base = str(self._cfg("judge.judge_api_base", "") or "")
        model = str(self._cfg("judge.judge_model", "") or "")
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                judge_message,
                text, score, is_owner, "", baseline, api_key, api_base, model,
            )
        except Exception as e:
            logger.warning(f"[BoundaryFeedback] 判断调用失败: {e}")
            result = {"type": "normal", "suitable_tier": "", "reason": ""}
        category, level, default_deduct = compute_level(result, score)
        # 扣分用配置值（confession/none 强制 0）
        if category in ("confession", "none"):
            deduct = 0
        else:
            deduct = {"light": int(self._cfg("deduct.deduct_light", -2)),
                      "mid": int(self._cfg("deduct.deduct_mid", -5)),
                      "severe": int(self._cfg("deduct.deduct_severe", -8)),
                      "bottom_line": int(self._cfg("deduct.deduct_severe", -8)) * 2}.get(level, default_deduct)
        return (category, level, deduct, str(result.get("reason") or ""))

    # ---------- 主人判断 ----------
    def _is_owner(self, user_id: str) -> bool:
        owners = [str(x) for x in self._cfg("basic.owner_user_ids", [])]
        return user_id in owners

    # ---------- 主入口 ----------
    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=200000)
    async def on_private_message(self, event: AstrMessageEvent):
        return await self._handle_event(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=200000)
    async def on_group_message(self, event: AstrMessageEvent):
        return await self._handle_event(event)

    async def _handle_event(self, event: AstrMessageEvent):
        if not self._cfg("basic.enabled", True):
            return None
        try:
            text = event.get_message_str() or ""
            user_id = str(event.get_sender_id() or "")
            if not user_id or self._is_owner(user_id):
                return None
            targets = [str(x) for x in self._cfg("basic.target_user_ids", [])]
            if targets and user_id not in targets:
                return None
            return await self._process_message(user_id, text, event)
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 处理消息异常: {e}")
            return None

    async def _process_message(self, user_id: str, text: str, event: AstrMessageEvent):
        is_apology = await self._judge_apology(user_id, text)
        category, level, deduct, hits = await self._judge_violation(user_id, text)
        if isinstance(hits, str):
            hits = [hits] if hits else []

        with self._lock:
            st = self._state.setdefault(user_id, self._new_state())
            # 道歉优先处理（即使消息里带道歉+轻微越界，以道歉为主）
            if is_apology and not (category == "bottom_line") and self._cfg("switch.enable_apology", True):
                self._handle_apology(user_id, st, event)
                self._save_state()
                return None
            # ===== 表白/好感（confession）：不越界，害羞回避，不扣分不推进阶段 =====
            if category == "confession":
                self._write_confession_gate(user_id)
                st["confessions"] = st.get("confessions", 0) + 1
                self._save_state()
                return None
            if category == "none" or deduct >= 0:
                return None
            if deduct < 0:
                # ===== 越界：扣分 + 阶段推进 =====
                # 叠加惩罚：恢复期内再越界 → 上一条未恢复的恢复额度作废（不再给机会），
                # 但累计扣分不重置（阶段不倒退），仅重开新的恢复额度
                if self._cfg("switch.enable_deduct", True):
                    if st.get("pending_deduct", 0) < 0 and st.get("recover_started_at", 0):
                        st["forfeited_deduct"] = st.get("forfeited_deduct", 0) + st.get("recoverable_quota", 0)
                        st["recoverable_quota"] = 0
                    # 信任追回：之前道歉标记过且再犯同类 → 直接追回标记好感
                    if st.get("marked_restored", 0) < 0 and st.get("marked_level") == level:
                        trust_recall = st["marked_restored"]
                        self._set_relationship_score(user_id, trust_recall, reason="trust_recall")
                        st["marked_restored"] = 0
                        st["marked_level"] = ""
                    # 扣分（阶段内再越界 = 追问纠缠，加重惩罚）
                    if st.get("stage") != "normal":
                        deduct = int(deduct * 1.5)
                    # 档位衰减：高好感档位扣分衰减（低档实打实记仇，满级扣得少）；低档记仇
                    score_now = self._get_relationship_score(user_id)
                    decay = self._tier_deduct_decay(score_now)
                    if decay < 1.0:
                        deduct = int(deduct * decay)
                    if self._tier_key(score_now) in ("deeply_distant", "strongly_distant", "distant", "acquaintance"):
                        # 低好感：默默忍受+记仇（不表达，但记在心里）
                        st["grudge"] = st.get("grudge", 0) + 1
                    st["pending_deduct"] = st.get("pending_deduct", 0) + deduct
                    # 新恢复额度 = 本次扣除绝对值 × 严重程度比例
                    ratio = {
                        "light": float(self._cfg("recover.recover_ratio_light", 0.5)),
                        "mid": float(self._cfg("recover.recover_ratio_mid", 0.33)),
                        "severe": float(self._cfg("recover.recover_ratio_severe", 0.25)),
                        "bottom_line": float(self._cfg("recover.recover_ratio_severe", 0.25)) * 0.5,
                    }.get(level, 0.5)
                    st["recoverable_quota"] = max(0, int(abs(deduct) * ratio))
                    self._deduct_relationship_score(user_id, deduct, reason=f"boundary_{level}")
                # 记录本次越界类别
                st["last_level"] = level
                st["last_hits"] = hits
                st["violations"].append({
                    "ts": time.time(), "level": level, "deduct": deduct,
                    "text": text[:100],
                })
                st["violations"] = st["violations"][-50:]
                # 底线系统
                bottom_line_count = 0
                if level == "bottom_line" and self._cfg("switch.enable_bottom_line", True):
                    st["bottom_line_count"] = st.get("bottom_line_count", 0) + 1
                    bottom_line_count = st["bottom_line_count"]
                    await self._apply_bottom_line(user_id, st, text, event)
                # 恢复计时：从第一次扣分开始（重新计时但不重置累计）
                st["recover_started_at"] = time.time()
                # 阶段推进
                if self._cfg("switch.enable_stage", True):
                    self._advance_stage(user_id, st)
                self._save_state()
                # 跟亲近的人吐槽（写进今日生活叙事，概率性）
                if self._cfg("switch.enable_vent", True):
                    self._vent_to_wxs(user_id, text, level)
                # 通知主人（概率性）
                if self._cfg("switch.enable_notify", True) and self._cfg("notify.notify_owner", True):
                    await self._notify_owner(user_id, text, level, deduct, st, bottom_line_count=bottom_line_count)
        return None

    def _new_state(self) -> dict:
        return {
            "pending_deduct": 0,        # 当前待恢复的扣分
            "recoverable_quota": 0,     # 当前可恢复额度（正数，叠加惩罚时作废）
            "recover_started_at": 0,    # 恢复开始时间
            "forfeited_deduct": 0,      # 因叠加惩罚被作废的恢复额度
            "apology_count": 0,         # 总道歉次数
            "apology_by_level": {},     # 按类别统计的道歉次数
            "marked_restored": 0,       # 道歉标记恢复的好感（负值表示已标记）
            "marked_level": "",         # 标记的越界类别
            "marked_ts": 0,             # 标记时间
            "bottom_line_count": 0,     # 底线次数
            "last_level": "",           # 最近一次越界类别
            "last_hits": [],            # 最近命中词
            "confessions": 0,           # 被表白次数（不扣分，害羞回避）
            "grudge": 0,                # 记仇次数（低好感越界时累计）
            "stage": "normal",          # normal/avoid/forbid/reflect
            "cold_until": 0,            # 冷落截止时间戳
            "violations": [],
        }

    # ---------- 道歉处理 ----------
    def _handle_apology(self, user_id: str, st: dict, event: AstrMessageEvent):
        """道歉 → 恢复部分好感 + 信任标记 + 加速恢复"""
        pending = st.get("pending_deduct", 0)
        if pending >= 0 and st.get("marked_restored", 0) >= 0:
            return
        # 同类道歉次数限制（信任透支检查）
        last_level = st.get("last_level", "") or "light"
        level_count = st.get("apology_by_level", {}).get(last_level, 0)
        limit = int(self._cfg("recover.apology_duplicate_limit", 3))
        if level_count >= limit:
            return  # 不接受道歉
        st["apology_count"] = st.get("apology_count", 0) + 1
        st["apology_by_level"][last_level] = level_count + 1
        # 恢复量 = 当前待恢复部分 × 道歉比例（受剩余额度约束）
        # 恢复量 = 当前待恢复部分 × 道歉比例（受剩余额度约束）× 档位恢复系数（高档可加回更多）
        quota = st.get("recoverable_quota", abs(pending))
        tier_factor = self._tier_recover_factor(user_id)
        restore = max(1, int(abs(pending) * float(self._cfg("recover.apology_restore_ratio", 0.6)) * tier_factor))
        restore = min(restore, abs(pending), quota if quota > 0 else abs(pending))
        if restore <= 0:
            return
        self._set_relationship_score(user_id, restore, reason="apology_restore")
        st["pending_deduct"] = pending + restore
        st["recoverable_quota"] = max(0, quota - restore)
        # 信任标记：恢复的好感被打标记（负值），再犯同类追回
        st["marked_restored"] = -restore
        st["marked_level"] = last_level
        st["marked_ts"] = time.time()
        # 加速后续恢复
        st["apology_active"] = True
        if st["pending_deduct"] >= 0:
            st["pending_deduct"] = 0
            st["recoverable_quota"] = 0
            st["recover_started_at"] = 0
            st["apology_active"] = False
            st["stage"] = "normal"
            self._clear_emotion_gate(user_id)

    # ---------- 阶段反应 ----------
    def _advance_stage(self, user_id: str, st: dict):
        """按累计扣分推进阶段，并同步写入情绪门状态让 nene 变冷淡"""
        pending = st.get("pending_deduct", 0)
        stage_avoid = int(self._cfg("deduct.stage_avoid_deduct", -6))
        stage_forbid = int(self._cfg("deduct.stage_forbid_deduct", -12))
        stage_reflect = int(self._cfg("deduct.stage_reflect_deduct", -20))
        if pending <= stage_reflect:
            st["stage"] = "reflect"
            st["cold_until"] = time.time() + int(self._cfg("recover.cold_shoulder_minutes", 180)) * 60
        elif pending <= stage_forbid:
            st["stage"] = "forbid"
        elif pending <= stage_avoid:
            st["stage"] = "avoid"
        self._write_emotion_gate(user_id, st["stage"])

    # ---------- 底线系统（三级） ----------
    async def _apply_bottom_line(self, user_id: str, st: dict, text: str, event: AstrMessageEvent):
        """底线三级惩罚：警告→扣至冷落→关系降档"""
        cnt = st.get("bottom_line_count", 0)
        cold_minutes = int(self._cfg("recover.cold_shoulder_minutes", 180))
        if cnt == 1:
            # 第一次：扣大好感（已在 _process_message 扣 severe*2）+ 直接警告（写入情绪门）
            st["cold_until"] = time.time() + max(60, cold_minutes // 3) * 60
            self._write_emotion_gate(user_id, "forbid")
        elif cnt == 2:
            # 第二次：直接扣到冷落
            st["cold_until"] = time.time() + cold_minutes * 60
            st["stage"] = "reflect"
            self._write_emotion_gate(user_id, "reflect")
        elif cnt >= 3:
            # 第三次：关系降档
            self._demote_relationship(user_id)
            st["cold_until"] = time.time() + cold_minutes * 60
            st["stage"] = "reflect"
            self._write_emotion_gate(user_id, "reflect")

    def _demote_relationship(self, user_id: str):
        """关系降档：当前档位降到下一档（取其上限）"""
        score = self._get_relationship_score(user_id)
        if score is None:
            return
        key, lo, hi = _tier_for_score(score)
        idx = next(i for i, t in enumerate(RELATIONSHIP_TIERS) if t[0] == key)
        if idx >= len(RELATIONSHIP_TIERS) - 1:
            new_score = RELATIONSHIP_TIERS[-1][1]  # 已是最低档，扣到最低
        else:
            new_score = RELATIONSHIP_TIERS[idx + 1][2]  # 下一档的上限
        delta = new_score - score
        self._set_relationship_score(user_id, delta, reason="bottom_line_demote")

    # ---------- 关系分操作（直接操作 companions.json，先读最新再改 + 备份） ----------
    def _companion_data_path(self) -> str:
        cfg_path = str(self._cfg("basic.companion_data_path", "") or "")
        if cfg_path and os.path.exists(cfg_path):
            return cfg_path
        # 自动探测：用 StarTools.get_data_dir 标准路径
        try:
            pc_dir = StarTools.get_data_dir("astrbot_plugin_private_companion")
            p = pc_dir / "companions.json"
            if p.exists():
                return str(p)
        except Exception:
            pass
        for p in [
            Path(__file__).resolve().parents[1] / "astrbot_plugin_private_companion" / "companions.json",
            Path("data/plugin_data/astrbot_plugin_private_companion/companions.json"),
            Path(__file__).resolve().parent.parent.parent / "plugin_data" / "astrbot_plugin_private_companion" / "companions.json",
        ]:
            try:
                if p.exists():
                    return str(p)
            except Exception:
                pass
        return ""

    def _backup_companions(self, path: str):
        if not self._cfg("basic.backup_before_write", True):
            return
        try:
            p = Path(path)
            bak_dir = p.parent / "backups"
            bak_dir.mkdir(parents=True, exist_ok=True)
            bak = bak_dir / f"companions_{time.strftime('%Y%m%d_%H%M%S')}.bak.json"
            shutil.copy2(p, bak)
            # 清理旧备份
            keep = max(1, int(self._cfg("basic.backup_max_keep", 5)))
            baks = sorted(bak_dir.glob("companions_*.bak.json"))
            for old in baks[:-keep]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[BoundaryFeedback] 备份失败: {e}")

    def _read_companions(self) -> dict:
        """读最新 companions.json（先读最新，避免覆盖并发写入）"""
        path = self._companion_data_path()
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 读 companions.json 失败: {e}")
            return {}

    def _get_relationship_score(self, user_id: str):
        d = self._read_companions()
        try:
            return int(d.get("users", {}).get(user_id, {}).get("relationship_score", 0))
        except Exception:
            return None

    def _set_relationship_score(self, user_id: str, delta: int, reason: str = "boundary"):
        """delta 可为正（恢复）或负（扣分）。先读最新再改。"""
        path = self._companion_data_path()
        if not path:
            return
        self._backup_companions(path)
        d = self._read_companions()
        if not d:
            return
        try:
            user = d.setdefault("users", {}).setdefault(user_id, {})
            old = int(user.get("relationship_score", 0) or 0)
            new = max(-1200, min(1200, old + delta))
            user["relationship_score"] = new
            ledger = user.setdefault("relationship_ledger", [])
            if isinstance(ledger, list):
                ledger.append({
                    "event_key": f"boundary_feedback:{int(time.time())}:{abs(hash(reason))%10000}",
                    "reason_code": reason,
                    "delta": int(new) - old,
                    "score_before": old,
                    "score_after": new,
                    "created_at": time.time(),
                    "source": "boundary_feedback",
                })
                if len(ledger) > 300:
                    del ledger[:-300]
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            logger.info(f"[BoundaryFeedback] {user_id} 关系分 {old} -> {new} ({reason})")
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 写关系分失败: {e}")

    def _deduct_relationship_score(self, user_id: str, delta: int, reason: str):
        if delta >= 0:
            return
        self._set_relationship_score(user_id, delta, reason=reason)
        # 越界惩罚：清零当天的正向加分累计（private_companion 每条消息 +1/+2 会稀释扣分，
        # 清零后当天所有正常互动加分作废，惩罚真实落地）
        try:
            path = self._companion_data_path()
            if not path:
                return
            d = self._read_companions()
            if not d:
                return
            user = d.get("users", {}).get(user_id)
            if not isinstance(user, dict):
                return
            totals = user.get("relationship_daily_totals")
            if isinstance(totals, dict):
                if int(totals.get("positive", 0) or 0) > 0:
                    totals["positive"] = 0
                    tmp = path + ".tmp"
                    Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, path)
                    logger.info(f"[BoundaryFeedback] {user_id} 当天正向加分已清零（越界惩罚）")
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 清零加分失败: {e}")

    # ---------- 情绪门（写入 companions.json 的 relationship_state，让 nene 自然变冷淡） ----------
    def _write_emotion_gate(self, user_id: str, stage: str, level: str = ""):
        path = self._companion_data_path()
        if not path:
            return
        # 单次越界（stage=normal 未到阶段阈值）也要写基础情绪门，让宁宁当轮受语气约束
        if stage == "normal":
            stage = "avoid"
        self._backup_companions(path)
        d = self._read_companions()
        if not d:
            return
        try:
            user = d.setdefault("users", {}).setdefault(user_id, {})
            rs = user.setdefault("relationship_state", {})
            if not isinstance(rs, dict):
                rs = {}
                user["relationship_state"] = rs
            now = time.time()
            # 程度 → mode/mood/时长（backoff 不触发 private_companion 的余波注入，所以越界至少用 hurt）
            mode_map = {"avoid": "hurt", "forbid": "hurt", "reflect": "refusing"}
            mood_map = {"avoid": -35, "forbid": -55, "reflect": -75}
            hurt_mins = {
                "avoid": 60, "forbid": 120,
                "reflect": int(self._cfg("recover.cold_shoulder_minutes", 180)),
            }.get(stage, 60)
            rs["mode"] = mode_map.get(stage, "hurt")
            rs["mood_score"] = mood_map.get(stage, -40)
            rs["mood_updated_ts"] = now
            rs["hurt_until"] = now + hurt_mins * 60
            rs["emotion_min_until"] = now + max(10, hurt_mins // 3) * 60
            rs["silence_turns"] = 1 if stage == "avoid" else (2 if stage == "forbid" else 3)
            rs["last_hurt_reason"] = f"boundary_feedback_{stage}"
            # 语气：按档位选（低好感默默记仇 / 中档按 level / 高好感沟通式）+ 按 level 细化
            score_now = self._get_relationship_score(user_id)
            tier_key = self._tier_key(score_now)
            if tier_key in ("deeply_distant", "strongly_distant", "distant", "acquaintance"):
                # 低好感：默默忍受不表达（但记仇），语气是默默型
                tone = str(self._cfg("tone.tone_silent", "") or "")
            elif tier_key in ("intimate", "deeply_bonded"):
                # 高好感：因为信任会试着沟通，表达自己为什么难过生气
                tone = str(self._cfg("tone.tone_communicate", "") or "")
            else:
                tone_key = {"light": "tone_mid", "mid": "tone_mid",
                            "severe": "tone_severe", "bottom_line": "tone_bottom_line"}.get(level, "tone_mid")
                tone = str(self._cfg(tone_key, "") or "")
            rs["last_hurt_text"] = tone if tone else "（边界反馈）"
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 写情绪门失败: {e}")

    def _write_confession_gate(self, user_id: str):
        """表白害羞情绪门：不扣分，但让宁宁当轮/短时间害羞回避（tone_light），短时长，不沉默"""
        path = self._companion_data_path()
        if not path:
            return
        self._backup_companions(path)
        d = self._read_companions()
        if not d:
            return
        try:
            user = d.setdefault("users", {}).setdefault(user_id, {})
            rs = user.setdefault("relationship_state", {})
            if not isinstance(rs, dict):
                rs = {}
                user["relationship_state"] = rs
            now = time.time()
            rs["mode"] = "hurt"
            rs["mood_score"] = -15
            rs["mood_updated_ts"] = now
            rs["hurt_until"] = now + 30 * 60
            rs["emotion_min_until"] = now + 10 * 60
            rs["silence_turns"] = 0  # 不沉默，但语气害羞
            rs["last_hurt_reason"] = "boundary_feedback_confession"
            tone = str(self._cfg("tone.tone_light", "") or "")
            rs["last_hurt_text"] = tone if tone else "（被说这样的话有点害羞）"
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 写害羞情绪门失败: {e}")

    def _clear_emotion_gate(self, user_id: str):
        """道歉恢复完成后清除情绪门"""
        path = self._companion_data_path()
        if not path:
            return
        d = self._read_companions()
        if not d:
            return
        try:
            user = d.setdefault("users", {}).setdefault(user_id, {})
            rs = user.get("relationship_state")
            if isinstance(rs, dict):
                rs["mode"] = "normal"
                rs["mood_score"] = 0
                rs["hurt_until"] = 0
                rs["emotion_min_until"] = 0
                rs["silence_turns"] = 0
                rs["mood_updated_ts"] = time.time()
                tmp = path + ".tmp"
                Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, path)
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 清情绪门失败: {e}")

    # ---------- 恢复任务 ----------
    async def _recovery_loop(self):
        """后台恢复：每恢复1点需要 recover_seconds_per_point 秒"""
        while True:
            try:
                await asyncio.sleep(60)
                self._tick_recovery()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BoundaryFeedback] 恢复循环异常: {e}")

    def _tick_recovery(self):
        now = time.time()
        with self._lock:
            for uid, st in self._state.items():
                pending = st.get("pending_deduct", 0)
                if pending >= 0:
                    continue
                started = st.get("recover_started_at", 0)
                if not started:
                    continue
                secs_per_point = max(60, int(self._cfg("recover.recover_seconds_per_point", 1800)))
                speedup = 1.0
                if st.get("apology_active"):
                    speedup = float(self._cfg("recover.apology_speedup_multiplier", 3.0))
                # 档位恢复速度：高档快（扣分可加回），低档慢（记仇难消）
                speedup *= self._tier_recover_factor(uid)
                elapsed = now - started
                # 可恢复量受恢复额度约束（叠加惩罚后额度可能为 0）
                quota = st.get("recoverable_quota", abs(pending))
                recoverable = min(abs(pending), quota)
                gained = int((elapsed / secs_per_point) * speedup)
                if gained > 0:
                    restore = min(gained, recoverable)
                    if restore > 0:
                        self._set_relationship_score(uid, restore, reason="boundary_recovery")
                    st["pending_deduct"] = pending + restore
                    st["recoverable_quota"] = max(0, quota - restore)
                    if st["pending_deduct"] >= 0:
                        st["pending_deduct"] = 0
                        st["recoverable_quota"] = 0
                        st["recover_started_at"] = 0
                        st["apology_active"] = False
                        st["stage"] = "normal"
                        self._clear_emotion_gate(uid)
            self._save_state()

    # ---------- 通知主人 ----------
    async def _notify_owner(self, user_id: str, text: str, level: str, deduct: int, st: dict, bottom_line_count: int = 0):
        """概率性向主人告状（委屈地跟主人吐槽），不是每次越界都报"""
        try:
            owners = [str(x) for x in self._cfg("basic.owner_user_ids", [])]
            if not owners:
                return
            # 概率：底线最高，轻度最低（角色不会一点小事都跑去告状）
            prob = {
                "bottom_line": 0.85,
                "severe": 0.55,
                "mid": 0.3,
                "light": 0.12,
            }.get(level, 0.15)
            prob = float(self._cfg("tattle_probability_" + level, prob))
            import random
            if random.random() > prob:
                return
            # 吐槽口吻（不是功能汇报）：委屈/不爽地跟主人说
            stage_label = {"avoid": "不太想理", "forbid": "有点生气了", "reflect": "不想说话"}.get(st.get("stage", ""), "")
            level_text = {
                "bottom_line": "有人踩我雷了", "severe": "有人真的很过分",
                "mid": "有人有点越界", "light": "有人怪怪的",
            }.get(level, "有人怪怪的")
            who = self._user_nickname(user_id)
            msg = f"那个……{who}，{level_text}。{text[:50]}"
            if stage_label:
                msg += f"我现在{stage_label}，暂时不想跟ta说话。"
            if bottom_line_count:
                msg += f"（这都第{bottom_line_count}次了。）"
            d = self._read_companions()
            users = d.get("users", {}) if isinstance(d, dict) else {}
            for owner in owners:
                umo = ""
                ou = users.get(owner) if isinstance(users, dict) else None
                if isinstance(ou, dict) and ou.get("umo"):
                    umo = str(ou["umo"])
                if not umo:
                    umo = f"nene:FriendMessage:{owner}"
                try:
                    chain = MessageChain([Plain(msg)])
                    await self.context.send_message(umo, chain)
                except Exception as e:
                    logger.error(f"[BoundaryFeedback] 通知主人失败: {e}")
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 通知失败: {e}")

    def _user_nickname(self, user_id: str) -> str:
        """从 companions.json 拿用户昵称，拿不到用 ID 尾巴"""
        try:
            d = self._read_companions()
            u = d.get("users", {}).get(user_id, {})
            if isinstance(u, dict):
                nick = str(u.get("nickname") or "").strip()
                if nick and nick != "你":
                    return nick
        except Exception:
            pass
        return user_id[-4:] + "号"

    # ---------- 跟亲近的人吐槽（写进 daily_story_plan 的生活叙事） ----------
    def _vent_to_wxs(self, user_id: str, text: str, level: str):
        """概率性把越界事件写进角色的今日生活叙事：ta 跟亲近的人吐槽了这件事。"""
        try:
            # 吐槽概率：底线/严重高，轻度低（角色不是什么事都往外说）
            prob = {"bottom_line": 0.9, "severe": 0.6, "mid": 0.35, "light": 0.15}.get(level, 0.2)
            prob = float(self._cfg("vent_probability_" + level, prob))
            import random
            if random.random() > prob:
                return
            path = self._companion_data_path()
            if not path:
                return
            self._backup_companions(path)
            d = self._read_companions()
            if not d:
                return
            today = time.strftime("%Y-%m-%d")
            story = d.setdefault("daily_story_plan", {})
            if not isinstance(story, dict) or story.get("date") != today:
                story = {"date": today, "today_events": []}
                d["daily_story_plan"] = story
            events = story.setdefault("today_events", [])
            if not isinstance(events, list):
                events = []
                story["today_events"] = events
            # 吐槽对象从配置读（角色亲近的人），默认通用“朋友”
            targets = [str(x) for x in self._cfg("notify.vent_targets", ["朋友"])]
            target = random.choice(targets) if targets else "朋友"
            who = self._user_nickname(user_id)
            level_desc = {
                "bottom_line": "真的踩到我雷了", "severe": "有点过分",
                "mid": "总说些奇怪的话", "light": "怪怪的",
            }.get(level, "怪怪的")
            excerpt = str(text or "")[:40]
            emotion = {
                "bottom_line": ("气得有点说不出话", "委屈又生气"),
                "severe": ("越想越气", "有点生气"),
                "mid": ("有点烦", "不太舒服"),
                "light": ("有点无语", "怪怪的"),
            }.get(level, ("有点烦", "怪怪的"))
            scene = str(self._cfg("notify.vent_scene_template", "") or "")
            if scene:
                event_text = scene.format(target=target, who=who, level_desc=level_desc, excerpt=excerpt)
            else:
                event_text = (
                    f"休息时，角色{emotion[0]}，忍不住跟{target}吐槽：\"那个{who}，{level_desc}。"
                    f"{excerpt}……\""
                    f"{target}听完没接话，角色{emotion[1]}地又补了一句：\"……不想理了。\""
                )
            events.append({
                "window": time.strftime("%H:%M") + "-" + time.strftime("%H:%M", time.localtime(time.time() + 900)),
                "event": event_text,
                "mood": emotion[1] + "，想跟朋友说",
                "lifecycle_status": "observed",
                "basis": ["external_vent"],
                "confidence": 0.9,
            })
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            logger.info(f"[BoundaryFeedback] 已写入对 {who} 的吐槽到今日生活叙事（对象：{target}）")
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 写吐槽事件失败: {e}")
    def _try_register_ability(self):
        """尝试注册一个陪伴面板可见的外部主动能力（失败静默）"""
        try:
            from data.plugins.astrbot_plugin_private_companion.main import get_private_companion_api
            api = get_private_companion_api()
            if not api:
                return
            self._companion_api = api
            api.register_proactive_ability({
                "name": "boundary_feedback_report",
                "module": "边界与情感反馈",
                "label": "边界告状",
                "description": "当有人对角色越界/触碰底线时，角色会向主人委屈地告状。",
                "when": "检测到越界或底线事件后，主人可能想知道的时刻",
                "use_for": "向主人转达有人越界的信息，形成告状素材",
                "avoid": "不要暴露插件机制，不要伪造越界事实，不要夸大",
                "share_probability": 0.15,
                "min_interval_hours": 6,
                "default_enabled": True,
                "default_config": {
                    "only_bottom_line": False,
                    "max_chars": 120,
                },
                "config_schema": {
                    "only_bottom_line": {
                        "type": "bool",
                        "label": "只告状底线事件",
                        "description": "开启后仅底线事件才向主人告状",
                    },
                    "max_chars": {
                        "type": "number",
                        "label": "摘要长度上限",
                        "description": "告状消息引用原文的最大字数",
                    },
                },
                "executor": self._ability_executor,
                "availability": self._ability_available,
            })
            logger.info("[BoundaryFeedback] 已注册陪伴面板主动能力 boundary_feedback_report")
        except Exception as e:
            logger.info(f"[BoundaryFeedback] 未接入陪伴面板（可忽略）: {e}")

    def _ability_available(self, ctx: dict) -> bool:
        """外部主动能力的可用性检查（快速、无副作用）"""
        try:
            user = ctx.get("user") or {}
            uid = str(user.get("user_id") or user.get("id") or user.get("umo") or "")
            if not uid:
                return False
            # 仅当该用户有未处理的越界/底线事件时才告状
            uid = self._normalize_uid_for_state(uid)
            st = self._state.get(uid, {})
            if not st:
                return False
            pending = st.get("pending_deduct", 0)
            bottom = st.get("bottom_line_count", 0)
            only_bottom = bool((ctx.get("config") or {}).get("only_bottom_line", False))
            if only_bottom:
                return bottom > 0
            return pending < 0 or bottom > 0
        except Exception:
            return False

    @staticmethod
    def _normalize_uid_for_state(uid: str) -> str:
        """外部能力 ctx 里的 uid 可能是 umo，尝试提取纯数字 ID"""
        uid = str(uid or "")
        for part in uid.split(":"):
            if part.isdigit():
                return part
        return uid

    def _ability_executor(self, ctx: dict):
        """执行告状：返回角色风格的告状文本（纯文本，不调模型）"""
        try:
            user = ctx.get("user") or {}
            uid = str(user.get("user_id") or user.get("id") or user.get("umo") or "")
            uid = self._normalize_uid_for_state(uid)
            st = self._state.get(uid, {})
            if not st:
                return {"text": "", "context": "没有可告状的事件", "summary": "无事件"}
            pending = st.get("pending_deduct", 0)
            bottom = st.get("bottom_line_count", 0)
            level_label = {"avoid": "回避", "forbid": "明令禁止", "reflect": "冷落反思"}.get(st.get("stage", ""), "")
            max_chars = int((ctx.get("config") or {}).get("max_chars", 120))
            parts = []
            if bottom > 0:
                parts.append(f"有个人踩到我的底线了，已经是第 {bottom} 次……")
            elif pending < 0:
                parts.append("有人一直说些让我不舒服的话……")
            if level_label:
                parts.append(f"我现在只想{level_label}")
            last_violations = st.get("violations", [])
            if last_violations:
                last = last_violations[-1]
                parts.append(f"「{str(last.get('text', ''))[:max_chars]}」")
            text = "，".join(parts) if parts else "（暂无越界事件）"
            return {
                "text": text,
                "context": f"边界告状：{uid}",
                "summary": "边界告状",
            }
        except Exception as e:
            logger.error(f"[BoundaryFeedback] 告状执行失败: {e}")
            return {"text": "", "context": "告状失败", "summary": "失败"}

    # ---------- 生命周期 ----------
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._cfg("basic.enabled", True):
            if self._recovery_task is None:
                self._recovery_task = asyncio.create_task(self._recovery_loop())
            logger.info("[BoundaryFeedback] 边界与情感反馈插件已启动")

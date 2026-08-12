# -*- coding: utf-8 -*-
"""
宁宁边界与情感反馈插件（astrbot_plugin_nene_boundary）

为 private_companion 补充边界反馈闭环：
- 越界行为（聊骚/恋爱向骚扰/恶意攻击）→ 扣 relationship_score（弹性恢复）
- 阶段反应：回避 → 明令禁止 → 反思（计时制，按累计扣分推进，写入情绪门让 nene 变冷淡）
- 道歉：恢复部分好感 + 信任标记，再犯同类追回
- 底线系统：恶意诋毁宁宁及其朋友 → 三级惩罚（警告/扣至冷落/关系降档）

零 LLM 调用：全部关键词/规则匹配 + 本地文件状态 + 定时器。

设计文档：OH-WorkSpace/nene边界与情感反馈系统设计.md
"""
import os
import json
import time
import asyncio
import random
import threading
import shutil
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.event.filter import EventMessageType
import astrbot.api.star as star
from astrbot.api.star import Context, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

try:
    from .judge_engine import judge_message, judge_apology, compute_level, NENE_BOTTOM_LINE_BASELINE
except ImportError:
    from judge_engine import judge_message, judge_apology, compute_level, NENE_BOTTOM_LINE_BASELINE  # 直接运行/测试时

# ============ 配置默认值 ============
DEFAULT_CFG = {
    "enabled": True,
    "deduct_light": -2,            # 轻度越界扣分
    "deduct_mid": -5,              # 中度越界扣分
    "deduct_severe": -8,           # 严重越界扣分
    "recover_seconds_per_point": 1800,   # 每恢复1点需要多少秒
    "recover_ratio_light": 0.5,    # 轻度越界可恢复比例
    "recover_ratio_mid": 0.33,     # 中度
    "recover_ratio_severe": 0.25,  # 严重
    "tier_deduct_decay": {
        # 好感档位 → 扣分衰减系数（低档实打实，满级只扣一半；扣分可加回的比例更高）
        "deeply_distant": 1.0, "strongly_distant": 1.0, "distant": 0.95,
        "acquaintance": 0.9, "familiar": 0.85, "close": 0.7,
        "intimate": 0.6, "deeply_bonded": 0.5,
    },
    "tier_recover_factor": {
        # 好感档位 → 恢复速度系数（高档恢复快，扣分能加回来；低档恢复慢，默默记仇）
        "deeply_distant": 0.5, "strongly_distant": 0.6, "distant": 0.7,
        "acquaintance": 0.8, "familiar": 0.9, "close": 1.0,
        "intimate": 1.25, "deeply_bonded": 1.5,
    },
    "tone_silent": "（低好感）会默默忍下来不表达，但心里记着，之后态度会有些不一样。",
    "tone_communicate": "（高好感）会因为信任而试着沟通，认真说出自己为什么难过、为什么生气，希望对方理解。",
    "stage_avoid_deduct": -6,      # 累计扣分达到 → 回避阶段
    "stage_forbid_deduct": -12,    # → 明令禁止阶段
    "stage_reflect_deduct": -20,   # → 反思/冷落阶段
    "apology_restore_ratio": 0.6,  # 道歉恢复比例（可恢复上限的60%）
    "apology_speedup_multiplier": 3.0,  # 道歉后恢复加速倍数
    "apology_duplicate_limit": 3,  # 同类道歉次数上限，超过不接受
    "bottom_line_keywords": [],    # 底线关键词（恶意诋毁等）
    "offend_keywords": [],         # 越界关键词（聊骚/恋爱向骚扰）
    "cold_shoulder_minutes": 180,  # 冷落期时长（分钟）
    "notify_owner": True,          # 越界/底线时通知主人（悠云）
    "tattle_llm_api_key": "",      # 告状文案 LLM key（空=不启用，用简单陈述；填了=宁宁自己组织语言）
    "tattle_llm_model": "deepseek-v4-flash",  # 告状 LLM 模型
    "tattle_llm_base": "https://api.deepseek.com/chat/completions",  # 告状 LLM 端点
    "judge_llm_api_key": "",       # 越界/道歉判断 LLM key（空=跳过 LLM 用关键词兜底）
    "judge_llm_model": "deepseek-v4-flash",  # 越界/道歉判断 LLM 模型
    "judge_llm_base": "https://api.deepseek.com/chat/completions",  # 越界/道歉判断 LLM 端点
    "target_user_ids": [],         # 生效的用户列表（空=所有非主人用户）
    "owner_user_ids": [],          # 主人（悠云）ID，越界检测对主人不生效
    "companion_data_path": "",     # private_companion 的 companions.json 路径（留空自动探测）
    "backup_before_write": True,   # 写 companions.json 前是否备份
    "backup_max_keep": 5,          # 备份文件最多保留份数
}

# 常见越界关键词（聊骚/恋爱向骚扰），可在配置里追加
DEFAULT_OFFEND_KEYWORDS = [
    "亲亲", "亲一个", "啵", "抱抱你", "想亲", "亲你", "贴贴", "腻歪",
    "亲爱的", "老婆", "老公", "嫁给我", "结婚", "你爱我", "爱不爱我",
    "睡你", "开房", "做爱", "上床", "摸你", "约吗",
    "你会一直爱我吗", "晚安吻", "我喜欢你", "我最喜欢你",
]

# 常见底线关键词（恶意诋毁/攻击宁宁及其朋友）
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


# 宁宁在意的人（用于识别"诋毁她的朋友"）
DEFAULT_CHERISHED_NAMES = [
    "宁宁", "草薙宁宁", "nene", "Nene", "笑梦", "花里笑梦", "花里",
    "天马司", "司", "神代类", "类", "悠云", "一歌", "小遥", "杏",
    "彰人", "实乃理", "爱莉", "穗波", "奏", "真冬", "绘名", "瑞希",
]

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


class NeneBoundaryPlugin(star.Star):
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
            val = getattr(self.config, key, default)
            return default if val is None else val
        except Exception:
            return default

    # ---------- 状态存储（本插件自己的边界状态） ----------
    def _init_state(self):
        """初始化状态存储路径（放在插件 data 目录）"""
        try:
            data_root = Path(StarTools.get_data_dir("astrbot_plugin_nene_boundary"))
        except Exception:
            data_root = Path("data") / "plugin_data" / "astrbot_plugin_nene_boundary"
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
            logger.warning(f"[NeneBoundary] 状态加载失败: {e}")
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
            logger.warning(f"[NeneBoundary] 状态保存失败: {e}")

    # ---------- 越界检测（纯规则，零 LLM） ----------
    def _detect_violation(self, text: str) -> tuple:
        """返回 (严重程度, 扣分值, 命中词列表)。level: none/light/mid/severe/bottom_line"""
        text = str(text or "").strip()
        if not text:
            return ("none", 0, [])
        offend_kws = list(DEFAULT_OFFEND_KEYWORDS) + [str(x) for x in self._cfg("offend_keywords", [])]
        bottom_kws = list(DEFAULT_BOTTOM_LINE_KEYWORDS) + [str(x) for x in self._cfg("bottom_line_keywords", [])]
        cherished = [str(x) for x in self._cfg("cherished_names", DEFAULT_CHERISHED_NAMES)]

        bottom_hits = [kw for kw in bottom_kws if kw and kw in text]
        # 底线：命中恶意词，且文本涉及宁宁或她在意的人
        mentions_cherished = any(name and name in text for name in cherished)
        if bottom_hits:
            if mentions_cherished:
                return ("bottom_line", self._cfg("deduct_severe", -8) * 2, bottom_hits)
            # 无针对对象时按严重度处理
            if len(bottom_hits) >= 2:
                return ("severe", self._cfg("deduct_severe", -8), bottom_hits)
            return ("mid", self._cfg("deduct_mid", -5), bottom_hits)
        # 越界词
        hits = [kw for kw in offend_kws if kw and kw in text]
        if len(hits) >= 3:
            return ("severe", self._cfg("deduct_severe", -8), hits)
        if len(hits) == 2:
            return ("mid", self._cfg("deduct_mid", -5), hits)
        if len(hits) == 1:
            return ("light", self._cfg("deduct_light", -2), hits)
        return ("none", 0, [])

    # ---------- 道歉判断（LLM 为主，关键词兜底） ----------
    async def _judge_apology(self, user_id: str, text: str) -> bool:
        """返回是否真诚道歉。LLM 判断；失败时用关键词兜底。"""
        api_key = str(self._cfg("judge_llm_api_key", "") or "").strip()
        api_base = str(self._cfg("judge_llm_base", "") or "")
        model = str(self._cfg("judge_llm_model", "") or "")
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, judge_apology, text, api_key, api_base, model,
            )
            if result:
                return True
            return self._detect_apology(text)
        except Exception:
            return self._detect_apology(text)

    # ---------- 道歉检测（关键词兜底） ----------
    def _detect_apology(self, text: str) -> bool:
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
        decay_map = self._cfg("tier_deduct_decay", {})
        if isinstance(decay_map, dict):
            key = self._tier_key(score)
            v = decay_map.get(key)
            if v:
                return max(0.3, min(1.0, float(v)))
        return 1.0

    def _tier_recover_factor(self, user_id: str) -> float:
        """档位恢复速度系数：高档恢复快（扣分可加回），低档慢（记仇难消）"""
        score = self._get_relationship_score(user_id)
        factor_map = self._cfg("tier_recover_factor", {})
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
        api_key = str(self._cfg("judge_llm_api_key", "") or "").strip()
        api_base = str(self._cfg("judge_llm_base", "") or "")
        model = str(self._cfg("judge_llm_model", "") or "")
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                judge_message,
                text, score, is_owner, "", api_key, api_base, model,
            )
        except Exception as e:
            logger.warning(f"[NeneBoundary] 判断调用失败: {e}")
            result = {"type": "normal", "suitable_tier": "", "reason": ""}
        category, level, default_deduct = compute_level(result, score)
        # 扣分用配置值（confession 强制 0）
        if category in ("confession", "none"):
            deduct = 0
        else:
            deduct = {"light": int(self._cfg("deduct_light", -2)),
                      "mid": int(self._cfg("deduct_mid", -5)),
                      "severe": int(self._cfg("deduct_severe", -8)),
                      "bottom_line": int(self._cfg("deduct_severe", -8)) * 2}.get(level, default_deduct)
        return (category, level, deduct, str(result.get("reason") or ""))

    # ---------- 主人判断 ----------
    def _is_owner(self, user_id: str) -> bool:
        owners = [str(x) for x in self._cfg("owner_user_ids", [])]
        return user_id in owners

    # ---------- 静音名单（掐断用：让 nene 说不出话，不影响外部监听） ----------
    def _is_muted(self, user_id: str) -> bool:
        try:
            import os as _os
            p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "mute_list.json")
            if not _os.path.isfile(p):
                return False
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            mutes = data.get("mutes", {}) if isinstance(data, dict) else {}
            if user_id not in mutes:
                return False
            until = mutes[user_id]
            if until:
                import time as _t
                if _t.time() > until:
                    return False
            return True
        except Exception:
            return False

    # ---------- 主入口 ----------
    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=200000)
    async def on_private_message(self, event: AstrMessageEvent):
        return await self._handle_event(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=200000)
    async def on_group_message(self, event: AstrMessageEvent):
        return await self._handle_event(event)

    async def _handle_event(self, event: AstrMessageEvent):
        if not self._cfg("enabled", True):
            return None
        try:
            text = event.get_message_str() or ""
            user_id = str(event.get_sender_id() or "")
            # 发送者真名（群名片/昵称，napcat 事件里直接带）
            sender_name = ""
            try:
                sender_name = str(event.get_sender_name() or "").strip()
            except Exception:
                pass
            if not user_id or self._is_owner(user_id):
                return None
            # 静音名单：直接吞掉消息（nene 说不出话，但消息事件仍会推送给外部监看）
            if self._is_muted(user_id):
                return None
            targets = [str(x) for x in self._cfg("target_user_ids", [])]
            if targets and user_id not in targets:
                return None
            # 群消息时取群号（用于告状指名群名）
            group_id = ""
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            try:
                parts = umo.split(":")
                if len(parts) >= 3 and "Group" in parts[1]:
                    group_id = parts[2]
            except Exception:
                pass
            return await self._process_message(user_id, text, event, group_id=group_id, sender_name=sender_name)
        except Exception as e:
            logger.error(f"[NeneBoundary] 处理消息异常: {e}")
            return None

    @filter.on_llm_request(priority=-500000)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """冷淡兜底注入：用户处于冷落/回避期时，直接往 prompt 追加宁宁的态度（不依赖 private_companion 的 band）"""
        try:
            if not self._cfg("enabled", True):
                return
            user_id = str(event.get_sender_id() or "")
            if not user_id or self._is_owner(user_id):
                return
            targets = [str(x) for x in self._cfg("target_user_ids", [])]
            if targets and user_id not in targets:
                return
            with self._lock:
                st = self._state.get(user_id)
            if not st:
                return
            stage = st.get("stage", "normal") or "normal"
            cold_until = float(st.get("cold_until", 0) or 0)
            # cold_until==0 表示无冷却截止（avoid/forbid 阶段）：只要没回到 normal 就持续冷淡
            if stage == "normal" or (cold_until > 0 and time.time() >= cold_until):
                return
            # 按阶段给宁宁的态度提示（不暴露机制，短句、带情绪）
            hint_map = {
                "avoid": "你现在有点不想搭理对方，回应要简短、疏远，不主动找话题。",
                "forbid": "你现在明确在生气/拒绝，话里要带出'别这样'的界限感，不顺着对方走。",
                "reflect": "你现在很受伤、不想说话，回应极简、冷淡，甚至带点刺，不轻易缓和。",
            }
            hint = hint_map.get(stage)
            if not hint:
                return
            if not hasattr(req, "system_prompt"):
                return
            current = req.system_prompt or ""
            marker = "\n【边界状态】"
            if marker in current:
                return
            req.system_prompt = f"{current}{marker}\n{hint}".strip()
            logger.info(f"[NeneBoundary] 冷淡注入: user={user_id}, stage={stage}")
        except Exception as e:
            logger.warning(f"[NeneBoundary] 冷淡注入失败: {e}")

    async def _process_message(self, user_id: str, text: str, event: AstrMessageEvent, group_id: str = "", sender_name: str = ""):
        is_apology = await self._judge_apology(user_id, text)
        category, level, deduct, hits = await self._judge_violation(user_id, text)
        if isinstance(hits, str):
            hits = [hits] if hits else []

        with self._lock:
            st = self._state.setdefault(user_id, self._new_state())
            # 道歉优先处理（即使消息里带道歉+轻微越界，以道歉为主）
            if is_apology and not (category == "bottom_line"):
                self._handle_apology(user_id, st, event)
                self._save_state()
                return None
            # ===== 表白/好感（confession）：不越界，宁宁害羞回避，不扣分不推进阶段 =====
            if category == "confession":
                self._write_confession_gate(user_id)
                st["confessions"] = st.get("confessions", 0) + 1
                self._save_state()
                return None
            if category in ("none",) or deduct >= 0:
                return None
            if deduct < 0:
                # ===== 越界：扣分 + 阶段推进 =====
                # 叠加惩罚：恢复期内再越界 → 上一条未恢复的恢复额度作废（不再给机会），
                # 但累计扣分不重置（阶段不倒退），仅重开新的恢复额度
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
                    # 已经因为越界处于冷淡/生气阶段，还继续越界 → 额外加重（追问纠缠）
                    deduct = int(deduct * 1.5)
                # 档位衰减：高好感档位扣分衰减（低档实打实记仇，满级扣得少）；低档记仇
                score_now = self._get_relationship_score(user_id)
                decay = self._tier_deduct_decay(score_now)
                if decay < 1.0:
                    deduct = int(deduct * decay)
                if self._tier_key(score_now) in ("deeply_distant", "strongly_distant", "distant", "acquaintance"):
                    # 低好感：宁宁默默忍受+记仇（不表达，但记在心里）
                    st["grudge"] = st.get("grudge", 0) + 1
                st["pending_deduct"] = st.get("pending_deduct", 0) + deduct
                # 新恢复额度 = 本次扣除绝对值 × 严重程度比例
                ratio = {
                    "light": float(self._cfg("recover_ratio_light", 0.5)),
                    "mid": float(self._cfg("recover_ratio_mid", 0.33)),
                    "severe": float(self._cfg("recover_ratio_severe", 0.25)),
                    "bottom_line": float(self._cfg("recover_ratio_severe", 0.25)) * 0.5,
                }.get(level, 0.5)
                st["recoverable_quota"] = max(0, int(abs(deduct) * ratio))
                applied = self._deduct_relationship_score(user_id, deduct, reason=f"boundary_{level}")
                # 实际落账量（分数上下限钳制时可能小于 deduct）——道歉/恢复以它为上界
                st["real_pending"] = st.get("real_pending", st["pending_deduct"]) + applied
                # 道歉后再犯同类：只认第一次道歉，再犯满 2 次 → 实时失望负面情绪 + 扣好感（情绪↔好感联动）
                if st.get("apology_accepted") and st.get("apology_level") == level:
                    st["apology_repeat_count"] = st.get("apology_repeat_count", 0) + 1
                    if st["apology_repeat_count"] >= int(self._cfg("apology_repeat_limit", 2)) and not st.get("apology_repeat_flagged"):
                        st["apology_repeat_flagged"] = True
                        extra = int(self._cfg("apology_repeat_penalty", -6))
                        applied_extra = self._set_relationship_score(user_id, extra, reason="apology_repeat_disappointment")
                        st["pending_deduct"] = st.get("pending_deduct", 0) + extra
                        st["real_pending"] = st.get("real_pending", st["pending_deduct"]) + applied_extra
                        self._write_disappointment_gate(user_id)
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
                if level == "bottom_line":
                    st["bottom_line_count"] = st.get("bottom_line_count", 0) + 1
                    bottom_line_count = st["bottom_line_count"]
                    await self._apply_bottom_line(user_id, st, text, event)
                # 恢复计时：从第一次扣分开始（重新计时但不重置累计）
                st["recover_started_at"] = time.time()
                # 联动双向：越界行为实时注入负面情绪（temp_emotion 情绪池）
                await self._inject_emotion_to_temp(user_id, level, st)
                # 阶段推进
                self._advance_stage(user_id, st, level)
                if level == "severe":
                    st["severe_count"] = st.get("severe_count", 0) + 1
                self._save_state()
                # 跟 WxS 成员吐槽（写进今日生活叙事，宁宁的日常圈子）
                self._vent_to_wxs(user_id, text, level)
                # 告状/吐槽：只对'很过分的话'（severe/bottom_line）才回头跟悠云说；light/mid 她自己消化
                if level in ("severe", "bottom_line") and self._cfg("notify_owner", True):
                    import asyncio as _aio
                    delay = random.uniform(600, 1200)
                    _aio.create_task(self._delayed_tattle(delay, user_id, text, level, deduct, st, bottom_line_count, group_id, sender_name))
        return None

    async def _delayed_tattle(self, delay: float, user_id: str, text: str, level: str, deduct: int, st: dict, bottom_line_count: int, group_id: str, sender_name: str):
        """话头结束后的延迟吐槽/告状：宁宁当场处理完，过一会儿才跟悠云提起"""
        import asyncio
        try:
            await asyncio.sleep(delay)
            await self._notify_owner(user_id, text, level, deduct, st, bottom_line_count=bottom_line_count, group_id=group_id, sender_name=sender_name)
        except Exception as e:
            logger.warning(f"[NeneBoundary] 延迟吐槽失败: {e}")

    def _new_state(self) -> dict:
        return {
            "pending_deduct": 0,        # 当前待恢复的扣分
            "real_pending": 0,          # 实际落账且未恢复的扣分（道歉/恢复以它为上界）
            "recoverable_quota": 0,     # 当前可恢复额度（正数，叠加惩罚时作废）
            "recover_started_at": 0,    # 恢复开始时间
            "forfeited_deduct": 0,      # 因叠加惩罚被作废的恢复额度
            "apology_count": 0,         # 总道歉次数
            "apology_by_level": {},     # 按类别统计的道歉次数
            "apology_accepted": False,  # 本话头是否已接受过一次道歉（只认第一次）
            "apology_level": "",        # 被接受道歉对应的越界类别
            "apology_repeat_count": 0,  # 道歉后再犯同类次数
            "apology_repeat_flagged": False,  # 是否已触发失望负面情绪
            "apology_hinted": False,    # 是否已提示“不用一直道歉”
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
        """道歉 → 只认第一次：首次恢复部分好感 + 信任标记 + 加速恢复（恢复量以实际扣分为上限）；
        重复道歉不再恢复不递减（次数照记）；第 3 次起提示“不用一直道歉”；
        道歉后再犯同类满 2 次 → 失望负面情绪 + 扣好感（情绪↔好感联动，实时触发回避）"""
        pending = st.get("pending_deduct", 0)
        if pending >= 0:
            return  # 无待恢复扣分（含 pending==0）不接受道歉恢复
        last_level = st.get("last_level", "") or "light"
        level_count = st.get("apology_by_level", {}).get(last_level, 0)
        st["apology_count"] = st.get("apology_count", 0) + 1
        st["apology_by_level"][last_level] = level_count + 1
        # 只认第一次：重复道歉不恢复、不递减
        if level_count > 0:
            if (level_count + 1) >= int(self._cfg("apology_hint_limit", 3)) and not st.get("apology_hinted"):
                st["apology_hinted"] = True
                self._write_apology_hint(user_id)
            return
        st["apology_accepted"] = True
        st["apology_level"] = last_level
        # 恢复量 = 当前待恢复部分 × 道歉比例 × 档位恢复系数（高档可加回更多）
        # 上限 = 实际落账且未恢复的扣分（分数下限/上限钳制导致实扣 < 记账时，不产生可恢复量）
        # 恢复只受实际扣分上限约束：recoverable_quota 是时间恢复额度，道歉不消耗它（防刷靠信任追回+只认第一次）
        real_pending = st.get("real_pending", pending)
        cap = abs(real_pending) if real_pending < 0 else 0
        tier_factor = self._tier_recover_factor(user_id)
        restore = max(1, int(abs(pending) * float(self._cfg("apology_restore_ratio", 0.6)) * tier_factor))
        restore = min(restore, abs(pending), cap)
        if restore <= 0:
            return
        self._set_relationship_score(user_id, restore, reason="apology_restore")
        st["pending_deduct"] = pending + restore
        st["real_pending"] = real_pending + restore
        # 信任标记：恢复的好感被打标记（负值），再犯同类追回
        st["marked_restored"] = -restore
        st["marked_level"] = last_level
        st["marked_ts"] = time.time()
        # 加速后续恢复
        st["apology_active"] = True
        if st["pending_deduct"] >= 0:
            st["pending_deduct"] = 0
            st["real_pending"] = 0
            st["recoverable_quota"] = 0
            st["recover_started_at"] = 0
            st["apology_active"] = False
            st["stage"] = "normal"
            self._clear_emotion_gate(user_id)
            self._reset_apology_context(st)


    # ---------- 阶段反应 ----------
    def _advance_stage(self, user_id: str, st: dict, level: str = ""):
        """按累计扣分推进阶段，并同步写入情绪门状态让 nene 变冷淡"""
        pending = st.get("pending_deduct", 0)
        stage_avoid = int(self._cfg("stage_avoid_deduct", -10))
        stage_forbid = int(self._cfg("stage_forbid_deduct", -18))
        stage_reflect = int(self._cfg("stage_reflect_deduct", -30))
        if pending <= stage_reflect:
            st["stage"] = "reflect"
            st["cold_until"] = time.time() + int(self._cfg("cold_shoulder_minutes", 180)) * 60
        elif pending <= stage_forbid:
            st["stage"] = "forbid"
        elif pending <= stage_avoid:
            st["stage"] = "avoid"
        self._write_emotion_gate(user_id, st["stage"], level)

    # ---------- 底线系统（三级） ----------
    async def _apply_bottom_line(self, user_id: str, st: dict, text: str, event: AstrMessageEvent):
        """底线三级惩罚：警告→扣至冷落→关系降档"""
        cnt = st.get("bottom_line_count", 0)
        cold_minutes = int(self._cfg("cold_shoulder_minutes", 180))
        if cnt == 1:
            # 第一次：扣大好感（已在 _process_message 扣 severe*2）+ 直接警告（写入情绪门）
            st["cold_until"] = time.time() + max(60, cold_minutes // 3) * 60
            self._write_emotion_gate(user_id, "forbid", "bottom_line")
        elif cnt == 2:
            # 第二次：直接扣到冷落
            st["cold_until"] = time.time() + cold_minutes * 60
            st["stage"] = "reflect"
            self._write_emotion_gate(user_id, "reflect", "bottom_line")
        elif cnt >= 3:
            # 第三次：关系降档
            self._demote_relationship(user_id)
            st["cold_until"] = time.time() + cold_minutes * 60
            st["stage"] = "reflect"
            self._write_emotion_gate(user_id, "reflect", "bottom_line")

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

    # ---------- private_companion 内存联动（文件写入只是兜底，内存才是它真正读的） ----------
    def _pc_plugin(self):
        """获取 private_companion 插件实例（同进程内直接改内存，让联动真正生效）"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_private_companion")
            if md and getattr(md, "star_cls", None):
                return md.star_cls
        except Exception:
            pass
        return None

    def _pc_sync_user(self, user_id: str, mutator):
        """在 private_companion 内存里更新指定用户的数据，并触发落盘"""
        try:
            plugin = self._pc_plugin()
            if not plugin:
                return False
            data = getattr(plugin, "data", None)
            if not isinstance(data, dict):
                return False
            users = data.setdefault("users", {})
            if not isinstance(users, dict):
                users = {}
                data["users"] = users
            user = users.setdefault(user_id, {})
            if not isinstance(user, dict):
                user = {}
                users[user_id] = user
            mutator(user)
            saver = getattr(plugin, "_schedule_default_data_save", None)
            if callable(saver):
                try:
                    saver(0)
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.warning(f"[NeneBoundary] 同步 private_companion 内存失败: {e}")
            return False

    def _pc_sync_interaction(self, user_id: str, band: str, reason: str, expires_at: float):
        """按 private_companion 的投影格式同步 current_interaction 到内存"""
        now = time.time()
        self._pc_sync_user(user_id, lambda u, _b=band, _r=reason, _e=expires_at, _n=now: u.__setitem__(
            "current_interaction",
            {
                "expression_band": _b,
                "band": _b,
                "band_source": "nene_boundary",
                "band_until": _e,
                "source": "automatic",
                "reason": _r,
                "updated_at": _n,
                "expires_at": _e,
                "manual_override": False,
            },
        ))

    # ---------- 关系分操作（直接操作 companions.json，先读最新再改 + 备份） ----------
    def _companion_data_path(self) -> str:
        cfg_path = str(self._cfg("companion_data_path", "") or "")
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
        if not self._cfg("backup_before_write", True):
            return
        try:
            p = Path(path)
            bak_dir = p.parent / "backups"
            bak_dir.mkdir(parents=True, exist_ok=True)
            bak = bak_dir / f"companions_{time.strftime('%Y%m%d_%H%M%S')}.bak.json"
            shutil.copy2(p, bak)
            # 清理旧备份
            keep = max(1, int(self._cfg("backup_max_keep", 5)))
            baks = sorted(bak_dir.glob("companions_*.bak.json"))
            for old in baks[:-keep]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[NeneBoundary] 备份失败: {e}")

    def _read_companions(self) -> dict:
        """读最新 companions.json（先读最新，避免覆盖并发写入）"""
        path = self._companion_data_path()
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.error(f"[NeneBoundary] 读 companions.json 失败: {e}")
            return {}

    def _get_relationship_score(self, user_id: str):
        d = self._read_companions()
        try:
            return int(d.get("users", {}).get(user_id, {}).get("relationship_score", 0))
        except Exception:
            return None

    def _set_relationship_score(self, user_id: str, delta: int, reason: str = "boundary") -> int:
        """delta 可为正（恢复）或负（扣分）。先读最新再改。返回实际落账增量。"""
        path = self._companion_data_path()
        if not path:
            return 0
        self._backup_companions(path)
        d = self._read_companions()
        if not d:
            return 0
        try:
            user = d.setdefault("users", {}).setdefault(user_id, {})
            old = int(user.get("relationship_score", 0) or 0)
            new = max(-1200, min(1200, old + delta))
            user["relationship_score"] = new
            ledger = user.setdefault("relationship_ledger", [])
            if isinstance(ledger, list):
                ledger.append({
                    "event_key": f"nene_boundary:{int(time.time())}:{abs(hash(reason))%10000}",
                    "reason_code": reason,
                    "delta": int(new) - old,
                    "score_before": old,
                    "score_after": new,
                    "created_at": time.time(),
                    "source": "nene_boundary",
                })
                if len(ledger) > 300:
                    del ledger[:-300]
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            logger.info(f"[NeneBoundary] {user_id} 关系分 {old} -> {new} ({reason})")
            self._pc_sync_user(user_id, lambda u, _v=new: u.__setitem__("relationship_score", _v))
            return int(new) - old
        except Exception as e:
            logger.error(f"[NeneBoundary] 写关系分失败: {e}")
            return 0

    def _deduct_relationship_score(self, user_id: str, delta: int, reason: str) -> int:
        if delta >= 0:
            return 0
        applied = self._set_relationship_score(user_id, delta, reason=reason)
        # 越界惩罚：清零当天的正向加分累计（private_companion 每条消息 +1/+2 会稀释扣分，
        # 清零后当天所有正常互动加分作废，惩罚真实落地）
        try:
            path = self._companion_data_path()
            if not path:
                return applied
            d = self._read_companions()
            if not d:
                return applied
            user = d.get("users", {}).get(user_id)
            if not isinstance(user, dict):
                return applied
            totals = user.get("relationship_daily_totals")
            if isinstance(totals, dict):
                if int(totals.get("positive", 0) or 0) > 0:
                    totals["positive"] = 0
                    tmp = path + ".tmp"
                    Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, path)
                    self._pc_sync_user(user_id, lambda u: (u.setdefault("relationship_daily_totals", {}) or {}).__setitem__("positive", 0))
                    logger.info(f"[NeneBoundary] {user_id} 当天正向加分已清零（越界惩罚）")
        except Exception as e:
            logger.error(f"[NeneBoundary] 清零加分失败: {e}")
        return applied

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
                "reflect": int(self._cfg("cold_shoulder_minutes", 180)),
            }.get(stage, 60)
            rs["mode"] = mode_map.get(stage, "hurt")
            rs["mood_score"] = mood_map.get(stage, -40)
            rs["mood_updated_ts"] = now
            rs["hurt_until"] = now + hurt_mins * 60
            rs["emotion_min_until"] = now + max(10, hurt_mins // 3) * 60
            # 同步写入 current_interaction.band：private_companion 用它驱动表达注入（relationship_state 已不驱动）
            ci = user.setdefault("current_interaction", {})
            if not isinstance(ci, dict):
                ci = {}
                user["current_interaction"] = ci
            ci["band"] = "hurt" if stage != "reflect" else "avoidant"
            ci["band_source"] = "nene_boundary"
            ci["band_until"] = now + hurt_mins * 60
            rs["silence_turns"] = 1 if stage == "avoid" else (2 if stage == "forbid" else 3)
            rs["last_hurt_reason"] = f"nene_boundary_{stage}"
            # 语气：按档位选（低好感默默记仇 / 中档按 level / 高好感沟通式）+ 按 level 细化
            score_now = self._get_relationship_score(user_id)
            tier_key = self._tier_key(score_now)
            if tier_key in ("deeply_distant", "strongly_distant", "distant", "acquaintance"):
                # 低好感：默默忍受不表达（但记仇），语气是默默型
                tone = str(self._cfg("tone_silent", "") or "")
            elif tier_key in ("intimate", "deeply_bonded"):
                # 高好感：因为信任会试着沟通，表达自己为什么难过生气
                tone = str(self._cfg("tone_communicate", "") or "")
            else:
                tone_key = {"light": "tone_mid", "mid": "tone_mid",
                            "severe": "tone_severe", "bottom_line": "tone_bottom_line"}.get(level, "tone_mid")
                tone = str(self._cfg(tone_key, "") or "")
            rs["last_hurt_text"] = tone if tone else "（边界反馈）"
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            # 同步到 private_companion 内存（文件会被它整写覆盖，内存才是它真正读的）
            self._pc_sync_interaction(
                user_id,
                "hurt" if stage != "reflect" else "avoidant",
                f"nene_boundary_{stage}",
                now + hurt_mins * 60,
            )
        except Exception as e:
            logger.error(f"[NeneBoundary] 写情绪门失败: {e}")

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
            rs["last_hurt_reason"] = "nene_boundary_confession"
            tone = str(self._cfg("tone_light", "") or "")
            rs["last_hurt_text"] = tone if tone else "（被说这样的话有点害羞）"
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._pc_sync_interaction(user_id, "hurt", "nene_boundary_confession", now + 30 * 60)
        except Exception as e:
            logger.error(f"[NeneBoundary] 写害羞情绪门失败: {e}")

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
                self._pc_sync_interaction(user_id, "relaxed", "nene_boundary_cleared", 0)
        except Exception as e:
            logger.error(f"[NeneBoundary] 清情绪门失败: {e}")

    # ---------- 恢复任务 ----------
    async def _inject_emotion_to_temp(self, user_id: str, level: str, st: dict):
        """联动双向：越界行为 → 实时注入负面情绪到 temp_emotion 情绪池（不是只扣好感）。
        按越界程度映射情绪与强度；阶段内再犯（stage != normal）时加重。"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_temp_emotion")
            te = getattr(md, "star_cls", None) if md else None
            if not te or not hasattr(te, "inject_external"):
                logger.warning(f"[NeneBoundary] temp_emotion 未找到/未激活，越界情绪注入跳过: user={user_id}, level={level}")
                return False
            plan = {
                "light": [("生气", 20), ("厌恶", 12)],
                "mid": [("生气", 30), ("厌恶", 22)],
                "severe": [("厌恶", 45), ("生气", 35)],
                "bottom_line": [("难过", 50), ("害怕", 45)],
            }.get(level)
            if not plan:
                return False
            mult = 1.5 if st.get("stage") not in ("normal", "") else 1.0
            for emo, s in plan:
                await te.inject_external(user_id, emo, min(100, int(s * mult)), f"nene_boundary_{level}", True)
            logger.info(f"[NeneBoundary] 越界情绪注入成功: user={user_id}, level={level}, plan={plan}, mult={mult}")
            return True
        except Exception as e:
            logger.warning(f"[NeneBoundary] 越界情绪注入失败: {e}")
            return False

    def _reset_apology_context(self, st: dict):
        """话头结束（恢复完成）重置道歉上下文：下次越界重新计数"""
        st["apology_accepted"] = False
        st["apology_level"] = ""
        st["apology_repeat_count"] = 0
        st["apology_repeat_flagged"] = False
        st["apology_hinted"] = False
        st["apology_by_level"] = {}

    def _write_apology_hint(self, user_id: str):
        """重复道歉提示：第 3 次及以上道歉 → 告诉对方不用一直道歉（轻量语气门，不覆盖冷落）"""
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
            if rs.get("mode") not in ("refusing", "avoidant"):
                rs["mode"] = "hurt"
            rs["mood_score"] = min(int(rs.get("mood_score", 0) or 0), -20)
            rs["mood_updated_ts"] = now
            rs["hurt_until"] = max(float(rs.get("hurt_until", 0) or 0), now + 60 * 60)
            rs["emotion_min_until"] = max(float(rs.get("emotion_min_until", 0) or 0), now + 20 * 60)
            rs["silence_turns"] = max(int(rs.get("silence_turns", 0) or 0), 0)
            rs["last_hurt_reason"] = "nene_boundary_apology_repeat"
            rs["last_hurt_text"] = "（无奈）不用一直道歉啦……我知道你在道歉，别反复说了，先好好说话。"
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._pc_sync_interaction(user_id, "hurt", "nene_boundary_apology_repeat", now + 60 * 60)
        except Exception as e:
            logger.error(f"[NeneBoundary] 写重复道歉提示失败: {e}")

    def _write_disappointment_gate(self, user_id: str):
        """道歉后再犯产生的失望负面情绪门：实时触发回避（轻量语气约束，不覆盖冷落阶段）"""
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
            if rs.get("mode") not in ("refusing", "avoidant"):
                rs["mode"] = "hurt"
            rs["mood_score"] = min(int(rs.get("mood_score", 0) or 0), -30)
            rs["mood_updated_ts"] = now
            rs["hurt_until"] = max(float(rs.get("hurt_until", 0) or 0), now + 90 * 60)
            rs["emotion_min_until"] = max(float(rs.get("emotion_min_until", 0) or 0), now + 30 * 60)
            rs["last_hurt_reason"] = "nene_boundary_apology_repeat_disappointed"
            rs["last_hurt_text"] = "（失望）道歉了却还是这样……嘴上说会改，事情还是一样。先别跟我说话了。"
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._pc_sync_interaction(user_id, "hurt", "nene_boundary_disappointed", now + 90 * 60)
        except Exception as e:
            logger.error(f"[NeneBoundary] 写失望情绪门失败: {e}")

    async def _recovery_loop(self):
        """后台恢复：每恢复1点需要 recover_seconds_per_point 秒"""
        while True:
            try:
                await asyncio.sleep(60)
                self._tick_recovery()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[NeneBoundary] 恢复循环异常: {e}")

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
                secs_per_point = max(60, int(self._cfg("recover_seconds_per_point", 1800)))
                speedup = 1.0
                if st.get("apology_active"):
                    speedup = float(self._cfg("apology_speedup_multiplier", 3.0))
                # 档位恢复速度：高档快（扣分可加回），低档慢（记仇难消）
                speedup *= self._tier_recover_factor(uid)
                elapsed = now - started
                # 可恢复量受恢复额度约束（叠加惩罚后额度可能为 0）
                # 且以实际落账的未恢复扣分为上限（避免钳制分被“恢复”成净加分）
                real_pending = st.get("real_pending", pending)
                cap = abs(real_pending) if real_pending < 0 else 0
                quota = st.get("recoverable_quota", 0)
                recoverable = min(abs(pending), quota, cap)
                gained = int((elapsed / secs_per_point) * speedup)
                if gained > 0:
                    restore = min(gained, recoverable)
                    if restore > 0:
                        self._set_relationship_score(uid, restore, reason="boundary_recovery")
                    st["pending_deduct"] = pending + restore
                    st["recoverable_quota"] = max(0, quota - restore)
                    if st["pending_deduct"] >= 0:
                        st["pending_deduct"] = 0
                        st["real_pending"] = 0
                        st["recoverable_quota"] = 0
                        st["recover_started_at"] = 0
                        st["apology_active"] = False
                        st["stage"] = "normal"
                        self._clear_emotion_gate(uid)
                        self._reset_apology_context(st)
            self._save_state()

    # ---------- 通知主人 ----------
    async def _notify_owner(self, user_id: str, text: str, level: str, deduct: int, st: dict, bottom_line_count: int = 0, group_id: str = "", sender_name: str = ""):
        """概率性向主人告状（宁宁委屈地跟悠云吐槽），指名道姓、带情绪、不念露骨原文"""
        try:
            owners = [str(x) for x in self._cfg("owner_user_ids", [])]
            if not owners:
                return
            # 概率：底线最高，轻度最低（宁宁不会一点小事都跑去告状）
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
            # 告状去重：同内容30分钟内不重复告状；同一人10分钟内不重复告状（宁宁不会同一个委屈翻来覆去说）
            import hashlib
            now_t = time.time()
            raw_hash = hashlib.md5(str(text or "").encode()).hexdigest()[:12]
            last = (st or {}).get("last_tattle") or {}
            if not isinstance(last, dict):
                last = {}
            last_same = last.get(raw_hash, 0)
            last_user = last.get("u_" + user_id, 0)
            if now_t - last_same < 1800 or now_t - last_user < 600:
                return
            st["last_tattle"] = last
            st["last_tattle"][raw_hash] = now_t
            st["last_tattle"]["u_" + user_id] = now_t
            # 对象指名：事件真名（群名片/昵称）→ companions → identities → 尾号
            who = (sender_name or "").strip() or self._user_nickname(user_id)
            gname = self._group_name(group_id) if group_id else ""
            target_desc = f"{gname}里那个{who}" if gname else who
            # 告状文案：优先宁宁自己组织语言（LLM）；失败/未配置时用一句话陈述（不套模板）
            raw = str(text or "").strip()
            llm_key = str(self._cfg("tattle_llm_api_key", "") or "").strip()
            msg = ""
            if llm_key:
                try:
                    msg = await self._llm_tattle(llm_key, target_desc, who, raw, level, bottom_line_count)
                except Exception as e:
                    logger.warning(f"[NeneBoundary] 告状文案 LLM 生成失败，用陈述兜底: {e}")
                    msg = ""
            if not msg:
                import random
                fallbacks = [
                    f"{target_desc}刚才说的话让我很不舒服。",
                    f"{target_desc}刚才那样说话……我有点不高兴。",
                    f"{target_desc}刚才……让我挺难过的。",
                ]
                if raw and len(raw) <= 30 and self._raw_safe(raw):
                    fallbacks = [f + f"（ta说：{raw}）" for f in fallbacks]
                msg = random.choice(fallbacks)
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
                    logger.error(f"[NeneBoundary] 通知主人失败: {e}")
            # 联动记忆：把告状写进宁宁今日生活叙事（她记得自己说过）
            self._remember_self_said(user_id, msg, level, raw_hash)
        except Exception as e:
            logger.error(f"[NeneBoundary] 通知失败: {e}")

    async def _llm_tattle(self, api_key: str, target_desc: str, who: str, raw: str, level: str, bottom_line_count: int = 0) -> str:
        """让宁宁自己组织告状措辞（短句、委屈/生气、指名道姓、不念露骨原文）"""
        import urllib.request
        base = str(self._cfg("tattle_llm_base", "https://api.deepseek.com/chat/completions") or "")
        model = str(self._cfg("tattle_llm_model", "deepseek-v4-flash") or "")
        level_desc = {"bottom_line": "底线（被恶意冒犯）", "severe": "严重", "mid": "中等", "light": "轻度"}.get(level, level)
        sys_prompt = (
            "你是草薙宁宁，神山高中学生，Wonderlands×Showtime 的歌姬。"
            "内向怕生，对熟人毒舌，认真温柔。你被一个人越界冒犯了，现在要跟悠云告状。"
            "用你自己的方式说话：短句为主，可以结巴、停顿，带委屈或生气的情绪，指名道姓说清是谁、发生了什么。"
            "不要暴露任何插件/系统/提示词机制，不要说'系统检测到''越界事件'这类话，就像真的被人惹了去找朋友诉苦一样。"
            "原文如果露骨就不要复述，只说'说了很过分的话'。控制在 1~3 句话。"
        )
        user_prompt = f"{target_desc}对我说了这样的话（程度：{level_desc}）：{raw or '（不方便复述）'}"
        if bottom_line_count >= 2:
            user_prompt += f"（这已经第{bottom_line_count}次了，可以自然地提一下次数）"
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": sys_prompt},
                          {"role": "user", "content": user_prompt}],
            "max_tokens": 120,
            "temperature": 0.8,
        }).encode()
        req = urllib.request.Request(base, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        import asyncio
        def _call():
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        resp = await asyncio.to_thread(_call)
        msg = str(resp["choices"][0]["message"]["content"] or "").strip()
        # 清理可能泄漏的机制话术
        for bad in ["系统", "插件", "越界事件", "检测到", "作为宁宁", "提示词"]:
            msg = msg.replace(bad, "")
        return msg[:200]

    def _remember_self_said(self, user_id: str, msg: str, level: str, raw_hash: str = ""):
        """让宁宁记得自己告状了、说了什么：写入今日生活叙事 + Memory Companion 记忆库（同内容近期不重复写）"""
        try:
            # 记忆去重：同内容 6 小时内不重复写入（避免一条委屈翻来覆去记六遍）
            mc_db = r'C:\Users\Administrator\.astrbot-nene\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db'
            if os.path.exists(mc_db) and raw_hash:
                try:
                    import sqlite3
                    conn = sqlite3.connect(mc_db, timeout=10)
                    row = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE content LIKE ? AND created_at > ?",
                        (f'%{msg[:30]}%', int(time.time()) - 21600)).fetchone()
                    conn.close()
                    if row and row[0]:
                        return  # 近期写过同样的告状，不重复
                except Exception:
                    pass
        except Exception:
            pass
        try:
            path = self._companion_data_path()
            if not path:
                return
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
            events.append({
                "ts": time.time(),
                "type": "vent",
                "text": f"跟悠云告状了：{msg}",
            })
            tmp = path + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"[NeneBoundary] 记录告状叙事失败: {e}")
        # 写入 Memory Companion：她检索"我告状过吗/我说了什么"时能看到
        try:
            mc_db = r'C:\Users\Administrator\.astrbot-nene\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db'
            if os.path.exists(mc_db):
                import sqlite3, uuid
                conn = sqlite3.connect(mc_db, timeout=10)
                now = int(time.time())
                mid = 'boundary_' + uuid.uuid4().hex[:12]
                content = f"我今天跟主人告状了，原话是：{msg}"
                conn.execute(
                    """INSERT INTO memories
                       (id, memory_type, subject_kind, subject_id, subject_name, subject_role,
                        object_kind, object_id, object_name, object_role,
                        scope, session_id, platform, visibility, sayability, reality_level, lifecycle,
                        content, confidence, importance, review_status, created_at, updated_at, occurred_at, source_plugin)
                       VALUES (?, 'bot_action', 'bot', 'self', '宁宁', 'bot',
                               'user', ?, '', 'user',
                               'private', '', 'qq', 'private', 'normal', 'real', 'active',
                               ?, 0.9, 0.8, 'none', ?, ?, ?, 'boundary_feedback')""",
                    (mid, str(user_id), content, now, now, now))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"[NeneBoundary] 写入告状记忆失败: {e}")

    def _user_nickname(self, user_id: str) -> str:
        """拿用户真名：companions 昵称 → Memory Companion identities → ID 尾号"""
        try:
            d = self._read_companions()
            u = d.get("users", {}).get(user_id, {})
            if isinstance(u, dict):
                nick = str(u.get("nickname") or "").strip()
                if nick and nick != "你":
                    return nick
        except Exception:
            pass
        # 查 Memory Companion identities（用户真名）
        try:
            mc_db = r'C:\Users\Administrator\.astrbot-nene\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db'
            if os.path.exists(mc_db):
                import sqlite3
                conn = sqlite3.connect(mc_db, timeout=5)
                row = conn.execute(
                    "SELECT display_name FROM identities WHERE entity_id = ? AND display_name != '' ORDER BY updated_at DESC LIMIT 1",
                    (str(user_id),)).fetchone()
                conn.close()
                if row and row[0]:
                    name = str(row[0]).strip()
                    if name and name != "你":
                        return name
        except Exception:
            pass
        return user_id[-4:] + "号"

    def _group_name(self, group_id: str) -> str:
        """从 Memory Companion identities 拿群名"""
        if not group_id:
            return ""
        try:
            mc_db = r'C:\Users\Administrator\.astrbot-nene\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db'
            if os.path.exists(mc_db):
                import sqlite3, re as _re
                conn = sqlite3.connect(mc_db, timeout=5)
                row = conn.execute(
                    "SELECT display_name FROM identities WHERE entity_id = ? LIMIT 1",
                    (str(group_id),)).fetchone()
                conn.close()
                if row and row[0]:
                    m = _re.search(r"Name:\s*([^\s].*?)(?:\s+Avatar|$)", str(row[0]))
                    if m:
                        return m.group(1).strip()
        except Exception:
            pass
        return ""

    # 原文是否含露骨/奇怪内容（决定告状是否复述原文）
    _RAW_FILTER_KWS = ["脱", "裸", "色", "睡", "做", "吻", "摸", "胸", "腿", "视频", "照片", "亲"]
    def _raw_safe(self, text: str) -> bool:
        return not any(k in text for k in self._RAW_FILTER_KWS)

    # ---------- 跟 WxS 成员吐槽（写进 daily_story_plan，宁宁的生活叙事） ----------
    def _vent_to_wxs(self, user_id: str, text: str, level: str):
        """概率性把越界事件写进宁宁的今日生活叙事：她跟类/司/笑梦吐槽了这件事。"""
        try:
            # 吐槽概率：底线/严重高，轻度低（宁宁不是什么事都往外说）
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
            # 取一个宁宁会吐槽的对象（类/司/笑梦），按她的人设偏好
            target = random.choice(["类", "司", "笑梦"])
            who = self._user_nickname(user_id)
            level_desc = {
                "bottom_line": "真的踩到我雷了", "severe": "有点过分",
                "mid": "总说些奇怪的话", "light": "怪怪的",
            }.get(level, "怪怪的")
            # 情绪按严重程度：委屈/生气/难受（宁宁会带情绪吐槽）
            emotion = {
                "bottom_line": ("气得有点说不出话", "委屈又生气"),
                "severe": ("越想越气", "有点生气"),
                "mid": ("有点烦", "不太舒服"),
                "light": ("有点无语", "怪怪的"),
            }.get(level, ("有点烦", "怪怪的"))
            excerpt = str(text or "")[:40]
            event_text = (
                f"排练间隙，宁宁{emotion[0]}，忍不住跟{target}吐槽：\"那个{who}，{level_desc}。"
                f"{excerpt}……\""
                f"{target}（{'挑了挑眉没接话' if target == '类' else '听完拍了拍她的肩' if target == '笑梦' else '认真听完皱了下眉' if target == '司' else '看了她一眼'}），"
                f"宁宁{emotion[1]}地又补了一句：\"……不想理了。\""
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
            logger.info(f"[NeneBoundary] 已写入宁宁对 {who} 的吐槽到今日生活叙事（对象：{target}）")
        except Exception as e:
            logger.error(f"[NeneBoundary] 写吐槽事件失败: {e}")
    def _try_register_ability(self):
        """尝试注册一个陪伴面板可见的外部主动能力（失败静默）"""
        try:
            from data.plugins.astrbot_plugin_private_companion.main import get_private_companion_api
            api = get_private_companion_api()
            if not api:
                return
            self._companion_api = api
            api.register_proactive_ability({
                "name": "nene_boundary_report",
                "module": "边界与情感反馈",
                "label": "边界告状",
                "description": "当有人对宁宁越界/触碰底线时，宁宁会向主人委屈地告状。",
                "when": "检测到越界或底线事件后，主人可能想知道的时刻",
                "use_for": "向主人（悠云）转达有人越界的信息，形成告状素材",
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
            logger.info("[NeneBoundary] 已注册陪伴面板主动能力 nene_boundary_report")
        except Exception as e:
            logger.info(f"[NeneBoundary] 未接入陪伴面板（可忽略）: {e}")

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
        """执行告状：返回宁宁风格的告状文本（纯文本，不调模型）"""
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
            logger.error(f"[NeneBoundary] 告状执行失败: {e}")
            return {"text": "", "context": "告状失败", "summary": "失败"}

    # ---------- 生命周期 ----------
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._cfg("enabled", True):
            if self._recovery_task is None:
                self._recovery_task = asyncio.create_task(self._recovery_loop())
            logger.info("[NeneBoundary] 边界与情感反馈插件已启动")

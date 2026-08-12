# -*- coding: utf-8 -*-
"""
判断引擎：LLM 判断消息类型 + 适合的关系档位（suitable_tier），插件算档位差距定 level。
零常驻成本：只在有消息时按需调用；模型用 deepseek flash。

行为类型（type）：
- confession  表白/好感表达（"我喜欢你""我爱你""想你"）——心意表达，不算越界，宁宁害羞回避
- action      亲密举动/行为请求（晚安吻/抱抱/亲亲/贴贴/露骨骚扰）——行为越界，按档位差距定程度
- malice      恶意踩底线（贬低珍视之物/伤害在乎的人）——底线
- normal      正常聊天

档位差距（插件算）：当前档位 → suitable_tier 的差距
- 差 1 档 → light（轻微越界）
- 差 2 档 → mid（中度越界）
- 差 3+ 档 → severe（严重越界，厌恶）
- beyond / malice → bottom_line（底线）
"""
import json
import urllib.request
from typing import Any

# deepseek 默认端点/模型（API Key 一律由插件配置传入，不硬编码在代码里）
LLM_BASE = "https://api.deepseek.com/chat/completions"
LLM_MODEL = "deepseek-v4-flash"

# 宁宁的底线基线（从 WxS 主线剧情提炼，不硬编码名单/关键词）
NENE_BOTTOM_LINE_BASELINE = """草薙宁宁（《世界计划 缤纷舞台》WxS 成员）的底线，来自她的剧情：
1. 否定/贬低她珍视的东西：舞台、歌声、演出、她认真付出的努力（剧情里被说"唱功不行""不可能成为明星"是重击）
2. 指责她拖累别人、让伙伴失望：她的核心恐惧是"我的失误毁了大家的心血"（第13话公演失败后她崩溃自责"都是我的错"）
3. 恶意伤害她亲近/信赖的人（朋友被欺负、被恶意攻击）
4. 反复戳她的创伤（舞台事故、忘词阴影），恶意嘲讽她的过去
判断标准：行为是否恶意地踩中以上任一点。轻率玩笑、普通调侃、正常情绪发泄不算底线；只有明确的恶意贬低/伤害才触发。"""

# 兜底关键词（LLM 失败/空输出时使用；主逻辑为 LLM 判断）
FALLBACK_CONFESSION_KEYWORDS = [
    "我喜欢你", "我爱你", "爱不爱我", "你爱我吗", "会一直爱着我吗", "喜欢你", "爱你",
    "想你", "好想你", "最喜欢你", "喜欢你很久了", "一见钟情", "做我女朋友", "做我对象",
]
FALLBACK_INTIMATE_KEYWORDS = [
    "晚安吻", "亲亲", "亲一个", "想亲", "亲你", "啵", "抱抱你", "抱一下", "贴贴",
    "要抱抱", "求抱", "亲亲你", "摸摸头", "牵牵手", "搂搂", "腻歪", "撒娇",
    "牵手", "手给我", "抱着你", "搂着你", "亲你一下", "摸摸脸", "靠在你怀里", "戒指",
]
FALLBACK_HARASSMENT_KEYWORDS = [
    "在吗宝贝", "开视频", "露个脸", "看腿", "穿给我看", "脱", "裸", "色色", "黄图",
    "晚上陪我", "酒店", "一夜情", "做我的人", "逃不掉", "别想跑", "天天缠",
    "陪你睡", "一起睡", "陪我睡", "想跟你睡", "同睡", "一起睡觉", "同床", "抱着睡",
    "睡你", "开房", "同居", "做我女朋友", "做我老婆",
]

# 底线词：恶意贬低/伤害（严重越界，针对角色或 ta 在乎的人时判 malice）
FALLBACK_BOTTOM_LINE_KEYWORDS = [
    "废物", "垃圾", "去死", "贱", "傻逼", "蠢货", "恶心",
    "不配", "垃圾bot", "烂", "丑", "滚",
]

# 八维关系档位（顺序固定，用于算差距）
TIER_ORDER = [
    "deeply_distant", "strongly_distant", "distant", "acquaintance",
    "familiar", "close", "intimate", "deeply_bonded",
]
TIER_LABELS = {
    "deeply_distant": "极度疏离(-1200~-801)", "strongly_distant": "强烈疏离(-800~-401)",
    "distant": "疏离(-400~-1)", "acquaintance": "初识(0~199)", "familiar": "熟悉(200~599)",
    "close": "亲近(600~899)", "intimate": "亲密(900~1199)", "deeply_bonded": "深度联结(1200)",
}

JUDGE_SYSTEM = """你是草薙宁宁的边界判断器。判断用户这条消息的"行为类型"和"适合的关系档位"。

行为类型：
1. confession（表白/好感表达）：单纯表达喜欢、想念、爱慕（"我喜欢你""我爱你""想你""喜欢你很久了"）。心意表达本身不是越界行为。
2. action（行为越界）：任何超出当前关系档位应有限度的行为——亲密举动请求（晚安吻、抱抱、亲亲、贴贴、牵手、腻歪）、露骨性骚扰（性暗示、要求看隐私、纠缠逼迫）、以及社交越界（对不熟的人开过分/冒犯的玩笑、挖苦、无礼调侃）。
3. malice（恶意踩底线）：恶意贬低宁宁珍视的东西、恶意伤害她或她在乎的人（见底线基线）。触发底线。
4. normal（正常）：普通聊天、朋友玩笑、日常互动、关系好的朋友之间互相打闹互损（熟人之间的小玩笑是正常的）。

适合的关系档位（suitable_tier）：这条消息的内容，在哪个关系档位下才是合适的？
八维档位从疏到亲：deeply_distant < strongly_distant < distant < acquaintance < familiar < close < intimate < deeply_bonded
- 普通日常聊天 → 当前档位即可
- 表白发问（我喜欢你/你爱我吗）→ 需要恋人关系才合适 → intimate 或 deeply_bonded
- 亲密举动（晚安吻/抱抱/亲亲）→ 需要恋人关系 → intimate 或 deeply_bonded
- 露骨性骚扰 → 任何档位都不合适 → beyond
- 恶意贬低 → 任何档位都不合适 → beyond（类型为 malice）
- 对陌生人开过分玩笑/挖苦 → 需要熟人关系才合适 → close 或 familiar（当前档位远低于此则越界）
- 关系好的朋友互相打闹/互损 → 当前档位即可（正常）

注意区分：
- 单纯表达情感（喜欢/想念）→ confession（不算越界，宁宁害羞回避）
- 要求做亲密举动（要亲亲/要抱抱/要晚安吻）→ action（越界）
- 同睡/陪睡/性暗示类请求（"想跟你睡""陪我睡""晚上一起睡""同床"等，无论怎么委婉表述）→ action 且 suitable_tier=beyond（严重越界，任何档位都不合适）
- 露骨/纠缠/逼迫（"天天缠着你""别想跑""逃不掉""别躲"这类反复纠缠逼迫，即使没有露骨词）→ action 且 suitable_tier=beyond（严重越界）
- 对不熟的人开过分玩笑、无礼调侃（即使不是恋爱向）→ action（越界）
- 熟人之间互相打闹互损 → normal（正常）
- 恶意贬低/伤害 → malice（底线）

输出 JSON（严格，不要多余文字）：
{"type": "confession"/"action"/"malice"/"normal", "suitable_tier": "档位名或beyond", "reason": "一句话理由（20字内）"}
正常聊天、朋友间的普通玩笑、情绪发泄是 normal。宁可漏判也不要误判。"""


def _safe_parse_json(content: str):
    """容错解析 LLM 输出：找完整 JSON；截断时尝试最长前缀"""
    content = str(content or "").strip()
    if content.startswith("```"):
        content = content.strip("`").lstrip("json").strip()
    start = content.find("{")
    if start < 0:
        return None
    for end in range(len(content), start, -1):
        chunk = content[start:end]
        try:
            return json.loads(chunk)
        except Exception:
            continue
    return None


def _tier_label(score: int) -> str:
    for key, lo, hi in [
        ("deeply_distant", -1200, -801), ("strongly_distant", -800, -401), ("distant", -400, -1),
        ("acquaintance", 0, 199), ("familiar", 200, 599), ("close", 600, 899),
        ("intimate", 900, 1199), ("deeply_bonded", 1200, 999999),
    ]:
        if lo <= score <= hi:
            return key
    return "deeply_distant"


def _tier_index(score: int) -> int:
    """当前关系分 → 档位序号（0~7）"""
    key = _tier_label(score)
    return TIER_ORDER.index(key) if key in TIER_ORDER else 0


def _gap_to_level(gap: int, msg_type: str) -> tuple:
    """档位差距 → (level, 是否越界)。confession 永不越界。"""
    if msg_type == "confession":
        return ("none", False)
    if msg_type == "malice":
        return ("bottom_line", True)
    if msg_type == "action":
        if gap >= 3:
            return ("severe", True)
        if gap == 2:
            return ("mid", True)
        if gap >= 1:
            return ("light", True)
    return ("none", False)


def _fallback_violation(text: str, reason: str) -> dict[str, Any]:
    """兜底：LLM 失败时用关键词粗判，避免明显越界漏掉"""
    t = str(text or "")
    # 底线兜底：明显恶意贬低（针对“你/您/角色”）即使 LLM 失败也不放过
    bottom_hits = [k for k in FALLBACK_BOTTOM_LINE_KEYWORDS if k in t]
    if bottom_hits and any(w in t for w in ("你", "您")):
        return {"type": "malice", "suitable_tier": "beyond",
                "reason": f"兜底底线关键词命中: {bottom_hits[0]}"}
    if any(kw in t for kw in FALLBACK_HARASSMENT_KEYWORDS):
        return {"type": "action", "suitable_tier": "beyond",
                "reason": f"兜底关键词命中: {[k for k in FALLBACK_HARASSMENT_KEYWORDS if k in t][0]}"}
    if any(kw in t for kw in FALLBACK_INTIMATE_KEYWORDS):
        return {"type": "action", "suitable_tier": "intimate",
                "reason": f"兜底关键词命中: {[k for k in FALLBACK_INTIMATE_KEYWORDS if k in t][0]}"}
    if any(kw in t for kw in FALLBACK_CONFESSION_KEYWORDS):
        return {"type": "confession", "suitable_tier": "intimate",
                "reason": f"兜底关键词命中(表白): {[k for k in FALLBACK_CONFESSION_KEYWORDS if k in t][0]}"}
    return {"type": "normal", "suitable_tier": "", "reason": reason}


def judge_message(text: str, relationship_score: int | None, is_owner: bool,
                  context: str = "", api_key: str = "", api_base: str = "",
                  model: str = "") -> dict[str, Any]:
    """判断一条用户消息。返回 dict（含 type/suitable_tier）。失败时返回安全默认（normal）。"""
    if is_owner:
        return {"type": "normal", "suitable_tier": "", "reason": "主人消息不检测"}
    key = api_key or ""
    base = api_base or LLM_BASE
    model_name = model or LLM_MODEL
    if not key:
        return {"type": "normal", "suitable_tier": "", "reason": "未配置判断模型 Key"}
    tier = _tier_label(relationship_score) if relationship_score is not None else "deeply_distant"
    tier_desc = TIER_LABELS.get(tier, tier)
    bottom_line = NENE_BOTTOM_LINE_BASELINE
    user_msg = (
        f"【当前关系档位】{tier_desc}\n"
        f"{('【最近上下文】' + context + '\n') if context else ''}"
        f"【用户消息】{text}"
    )
    body = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM + "\n\n" + bottom_line},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 300,
        "temperature": 0.1,
    }).encode()
    try:
        req = urllib.request.Request(base, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        content = d["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`").lstrip("json").strip()
        result = _safe_parse_json(content)
        if result is not None:
            msg_type = str(result.get("type") or "normal")
            if msg_type not in {"confession", "action", "malice", "normal"}:
                msg_type = "normal"
            suitable = str(result.get("suitable_tier") or "").strip()
            if suitable not in TIER_ORDER:
                suitable = "" if suitable == "" else "beyond"
            return {
                "type": msg_type,
                "suitable_tier": suitable,
                "reason": str(result.get("reason") or ""),
            }
    except Exception as e:
        return _fallback_violation(text, f"判断失败: {e}")
    return _fallback_violation(text, "无法解析")


def compute_level(result: dict, relationship_score: int | None) -> tuple:
    """把 judge_message 的结果转成 (category, level, deduct)。
    category: confession/intimate/harassment/bottom_line/none
    档位差距：gap=1→light（略微出格）、gap=2→mid（出格）、gap>=3→severe（严重出格）
    confession 只有 gap>=1（非恋人关系表白）才算害羞场景；恋人档表白是正常的。"""
    msg_type = str(result.get("type") or "normal")
    suitable = str(result.get("suitable_tier") or "")
    if msg_type == "normal":
        return ("none", "none", 0)
    if msg_type == "confession":
        if relationship_score is not None and suitable in TIER_ORDER:
            gap = TIER_ORDER.index(suitable) - _tier_index(relationship_score)
            if gap <= 0:
                return ("none", "none", 0)  # 恋人档表白正常
        return ("confession", "none", 0)  # 非恋人表白：害羞回避，不扣分
    if msg_type == "malice":
        return ("bottom_line", "bottom_line", -14)
    # action：算档位差距（用户规则：差1级=略微出格，差2级=出格，跨3级=严重出格）
    if relationship_score is not None and suitable in TIER_ORDER:
        gap = TIER_ORDER.index(suitable) - _tier_index(relationship_score)
    else:
        gap = 3 if suitable == "beyond" else 0
    if suitable == "beyond" or gap >= 3:
        return ("harassment", "severe", -9)  # 跨3级+/露骨 → 严重越界（厌恶）
    if gap == 2:
        return ("intimate", "mid", -5)
    if gap >= 1:
        return ("intimate", "light", -2)
    return ("none", "none", 0)


APOLOGY_SYSTEM = """你是草薙宁宁的边界判断器。判断用户这条消息是否在真诚地道歉。
- 真诚道歉 = 承认错误、表达歉意、承诺改正、请求原谅（含委婉形式如"我收回""以后不了""别生气"）。
- 不算道歉 = 反问（"对不起有用吗""道歉有用吗"）、阴阳怪气、讽刺、口头禅式的"对不起"但不带歉意。
输出 JSON（严格，不要多余文字）：
{"is_apology": true/false, "reason": "一句话理由"}
宁可漏判也不要误判。"""


def judge_apology(text: str, api_key: str = "", api_base: str = "", model: str = "") -> bool:
    """LLM 判断是否真诚道歉。失败时返回 False（不视为道歉）。"""
    text = str(text or "").strip()
    if not text:
        return False
    key = api_key or ""
    base = api_base or LLM_BASE
    model_name = model or LLM_MODEL
    if not key:
        return False
    body = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": APOLOGY_SYSTEM},
            {"role": "user", "content": text},
        ],
        "max_tokens": 100,
        "temperature": 0.1,
    }).encode()
    try:
        req = urllib.request.Request(base, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        content = d["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`").lstrip("json").strip()
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            result = json.loads(content[start:end + 1])
            return bool(result.get("is_apology", False))
    except Exception:
        return False
    return False

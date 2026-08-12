# -*- coding: utf-8 -*-
"""
astrbot_plugin_temp_emotion — 临时情绪系统 v5（分数 + 阶段制）
- 收到消息 → LLM 理解语义 → 情绪（17 种）+ 强度分（0-100）
- 情绪池：正面池 / 负面池（按情绪分列），净分 = 正池 - 负池
- 阶段：净分按可配置阈值分档（正负各 5 段，阈值可调）
- 抗性：当前阶段越高，反向情绪冲击越小（每阶段可配置减分值）
- 时间消解：每 N 分钟各池衰减 X 分
- 话头结束沉淀：池分达阈值 → 写入情绪日记（影响后续注入）
- 转移话题强制：被惹类负面 → 叠加生气；担忧类 → 担忧加分
- 回复注入：阶段 + 分数 + 来源（不预设反应话术）
"""
import asyncio
import json
import os
import re
import time
import urllib.request
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.api.event import filter
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# ===== 情绪维度 =====
# 正面池：开心/温暖/期待/安心/自信/惊喜/感动
# 负面池：生气/难过/害怕/厌恶/委屈/紧张/失望/嫉妒
# 复合池（可与任何情绪并存，不参与正负净分、相抵与抗性）：
#   害羞（又羞又恼）、担忧（替对方担心，与安心对应）、紧张（与安心对应，偏中性有积极/消极底色）
POSITIVE = {'开心', '温暖', '期待', '安心', '自信', '惊喜', '感动'}
NEGATIVE = {'生气', '难过', '害怕', '厌恶', '委屈', '失望', '嫉妒', '自卑'}
COMPLEX = {'害羞', '担忧', '紧张'}

# ===== 情绪影响图谱（扩散扣减） =====
# 每个情绪定义对池内其他情绪的影响权重：{情绪: [(目标情绪, 系数), ...]}
# 新情绪进入时：总伤害 = 本条分数，按权重加权分给命中的目标情绪（互斥对象权重最高，其他情绪被波及）
# 例如失望进来：期待被重扣（1.0）、开心也被波及（0.5）——扩散影响
# 涉及复合池的：担忧-开心（转化冻结）、担忧/紧张-安心（对应削减）走联动
# 未列出的组合默认共存（害羞可和一切并存：又羞又恼、开心又害羞；紧张和开心并存；担忧和难过并存；感动和难过并存）
DEFAULT_IMPACT_GRAPH = {
    '失望': [('期待', 1.0), ('开心', 0.5), ('自信', 0.4)],
    '难过': [('开心', 0.8), ('温暖', 0.5), ('自信', 0.4), ('期待', 0.4)],
    '害怕': [('安心', 1.0), ('期待', 0.5), ('自信', 0.3)],
    '生气': [('温暖', 0.6), ('开心', 0.4), ('安心', 0.3)],
    '厌恶': [('温暖', 0.8), ('开心', 0.4)],
    '委屈': [('开心', 0.6), ('温暖', 0.4)],
    '嫉妒': [('自信', 0.8), ('开心', 0.6)],
    '自卑': [('自信', 1.0), ('开心', 0.6), ('期待', 0.4)],
    '开心': [('难过', 0.8), ('失望', 0.5)],
    '温暖': [('厌恶', 0.8), ('生气', 0.4)],
    '期待': [('失望', 1.0)],
    '安心': [('害怕', 1.0)],
    '自信': [('自卑', 1.0), ('失望', 0.4)],
    '惊喜': [],
    '感动': [],
    '害羞': [],
    '担忧': [('开心', 1.0), ('期待', 0.7), ('自信', 0.6), ('安心', 1.0)],
    '紧张': [('安心', 1.0)],
}


_STOP_WORDS = set('的了呢吗吧啊呀哦哈是我你他她它们这那什么怎么一个一下没有不也就在跟和与或又还')


def _keywords(text: str) -> set:
    """提取 2 字以上中文关键词（过滤纯虚词）"""
    import re
    return {w for w in re.findall(r'[\u4e00-\u9fff]{2,}', text or '')
            if not all(c in _STOP_WORDS for c in w)}


def _emo_dir(e: str) -> str:
    """情绪方向：正面 / 负面（含担忧、紧张这类偏负复合）/ 中性（害羞）"""
    if e in POSITIVE:
        return 'pos'
    if e in NEGATIVE or e in ('担忧', '紧张'):
        return 'neg'
    return 'neu'


def _is_topic_change(prev_text: str, text: str, prev_emo: str | None, cur_emo: str | None) -> bool:
    """话题转移判定：情绪方向不同（上一条 vs 当前主情绪）+ 话题不同步（词面无共享关键词）
    注意：难过→担忧这类方向相同的延续不算转移（同一件事的共情连续）"""
    if prev_emo and cur_emo:
        if _emo_dir(prev_emo) == _emo_dir(cur_emo):
            return False
    # 词面：无共享关键词 = 话题不同步
    return not (_keywords(prev_text) & _keywords(text))


def _parse_emotion_thresholds(val: str = '') -> dict:
    """解析每情绪阈值：'开心=10,30,50,70,90; 难过=5,15,30,50,70' → {情绪: [阈值]}"""
    try:
        m = {}
        for entry in str(val or '').split(';'):
            entry = entry.strip()
            if '=' not in entry:
                continue
            e, rest = entry.split('=', 1)
            e = e.strip()
            items = [int(x.strip()) for x in rest.split(',') if str(x).strip().isdigit()]
            if items and (e in POSITIVE or e in NEGATIVE or e in COMPLEX):
                m[e] = items
        return m
    except Exception:
        return {}

# ===== 多段阻尼（每个情绪独立可调） =====
# 配置格式：'开心=20:1.0,40:0.75,60:0.55,80:0.4,100:0.3; 生气=20:1.0,40:0.8,60:0.6,80:0.45,100:0.35'
# 语义：该情绪分数 0~20 全额、20~40 打 75%、40~60 打 55%、60~80 打 40%、80~100 打 30%
# 作用：LLM 高分冲动被压回循序渐进，普通闲聊天然到不了高分段
DEFAULT_DAMP_CURVE = [(20, 1.0), (40, 0.75), (60, 0.55), (80, 0.4), (100, 0.3)]

# 普通对话单情绪注入上限：非事件消息每句某情绪最多注入这么多分（事件才有额外值）
DEFAULT_INJECT_CAP = 20


def _parse_damp_curve(val: str = '') -> dict:
    """解析多段阻尼：'开心=20:1.0,40:0.75; 生气=...' → {情绪: [(段上限, 系数), ...]}（未配置情绪用默认曲线）"""
    try:
        m = {}
        for entry in str(val or '').split(';'):
            entry = entry.strip()
            if '=' not in entry:
                continue
            e, rest = entry.split('=', 1)
            e = e.strip()
            items = []
            for seg in rest.split(','):
                seg = seg.strip()
                if ':' not in seg:
                    continue
                limit_s, factor_s = seg.split(':', 1)
                if not limit_s.strip().isdigit():
                    continue
                try:
                    factor = float(factor_s)
                except Exception:
                    continue
                items.append((int(limit_s.strip()), factor))
            if items and (e in POSITIVE or e in NEGATIVE or e in COMPLEX):
                items.sort(key=lambda x: x[0])
                m[e] = items
        return m
    except Exception:
        return {}


def _damp_curve_for(emotion: str, damp_map: dict | None) -> list:
    if damp_map:
        if emotion in damp_map:
            return damp_map[emotion]
        if '*' in damp_map:
            return damp_map['*']
    return DEFAULT_DAMP_CURVE


def _damp_score(score: int, curve: list) -> int:
    """多段阻尼压缩：每段按该段系数压缩（例：50 → 20*1.0 + 20*0.75 + 10*0.55 = 40）"""
    if score <= 0 or not curve:
        return score
    out = 0.0
    prev = 0
    for limit, factor in curve:
        if score <= prev:
            break
        seg = min(score, limit) - prev
        out += seg * factor
        prev = limit
        if score <= limit:
            break
    else:
        if score > prev:
            out += (score - prev) * curve[-1][1]
    return max(0, int(round(out)))


def _parse_inject_cap_map(val: str = '') -> dict:
    """解析每情绪注入上限：'开心=20,生气=15' → {情绪: 上限}（未配置情绪用默认 20）"""
    try:
        m = {}
        for part in str(val or '').split(','):
            part = part.strip()
            if '=' not in part:
                continue
            k, v = part.split('=', 1)
            k = k.strip()
            if k in POSITIVE or k in NEGATIVE or k in COMPLEX:
                try:
                    n = int(str(v).strip())
                except Exception:
                    n = DEFAULT_INJECT_CAP
                m[k] = max(1, min(100, n))
        return m
    except Exception:
        return {}


def _inject_cap_for(emotion: str, cap_map: dict | None) -> int:
    if cap_map and emotion in cap_map:
        return cap_map[emotion]
    return DEFAULT_INJECT_CAP


def _thr_for(emotion: str, pos_thr: list, neg_thr: list, neu_thr: list,
             et_map: dict | None = None) -> list:
    """按情绪取阶段阈值：每情绪配置优先，无则用池默认"""
    if et_map and emotion in et_map:
        return et_map[emotion]
    if emotion in POSITIVE:
        return pos_thr
    if emotion in NEGATIVE:
        return neg_thr
    return neu_thr


def _parse_impact_graph(val: str = '') -> dict:
    """解析影响图谱配置：'失望:期待=1.0,开心=0.5; 难过:开心=0.8'（空=默认图谱）"""
    try:
        raw = str(val or '').strip()
        if not raw:
            return DEFAULT_IMPACT_GRAPH
        g = {}
        for entry in raw.split(';'):
            entry = entry.strip()
            if ':' not in entry:
                continue
            src, rest = entry.split(':', 1)
            src = src.strip()
            items = []
            for it in rest.split(','):
                it = it.strip()
                if '=' in it:
                    t, r = it.split('=', 1)
                    t, r = t.strip(), r.strip()
                    if t and (t in POSITIVE or t in NEGATIVE or t in COMPLEX) and r.replace('.', '', 1).isdigit():
                        items.append((t, float(r)))
            if items and (src in POSITIVE or src in NEGATIVE or src in COMPLEX):
                g[src] = items
        if not g:
            return DEFAULT_IMPACT_GRAPH
        return g
    except Exception:
        return DEFAULT_IMPACT_GRAPH

def _group_of(emotion: str) -> str:
    if emotion in POSITIVE:
        return 'pos'
    if emotion in NEGATIVE:
        return 'neg'
    return 'neu'

# ===== 规则兜底表（LLM 不可用时的基本反应，不是主机制） =====
RULE_FALLBACK = [
    (('废物', '垃圾', '蠢货', '真笨', '好笨', '大笨蛋', '笨死了', '蠢死了', '滚', '恶心', '讨厌你', '烦死', '没用', '菜鸡', '白痴', '差劲', '丢人', '活该', '有病'), ('生气', 43)),
    (('鬼', '恐怖', '吓死', '好吓人', '有鬼', '幽灵', '灵异'), ('害怕', 31)),
    (('出事了', '出大事', '完了', '坏了', '糟糕'), ('紧张', 28)),
    (('冤枉', '误会', '凭什么', '又不是我', '不是我干的', '委屈'), ('委屈', 33)),
    (('好累', '想哭', '崩溃', '撑不住', '好痛苦', '好难受', 'emo', '不想活'), ('难过', 32)),
    (('对不起', '抱歉', '我错了', '是我的错', '原谅我', '别生气', '是我不好'), ('温暖', 30)),
    (('又这样', '白期待', '算了吧', '失望', '没意思', '放鸽子', '骗我', '说好的', '又没来', '爽约'), ('失望', 27)),
    (('比你好', '比不上', '差远了', '不如她', '你听听人家', '就你不行'), ('嫉妒', 29)),
    (('引退', '退役', '再也听不到', '永远离开'), ('难过', 25)),
    (('陪我睡', '跟我睡', '一起睡', '陪我过夜', '晚上陪你', '睡你', '开房', '上床', '摸你', '约吗'), ('厌恶', 40)),
    (('缠着你', '天天缠', '别想跑', '逃不掉', '威胁', '不然就', '不客气'), ('害怕', 34)),
    (('让我抱', '给我抱', '身上好软', '抱抱我', '让我亲', '让我摸'), ('厌恶', 30)),
    (('被骂', '挨骂', '被领导', '被老板', '被凶', '被欺负', '被批评', '被开除', '被辞退', '被裁员'), ('担忧', 26)),
    (('抱抱', '亲亲', '想你', '喜欢你', '爱你', '老婆', '亲爱的', '脱'), ('厌恶', 36)),
    (('好听', '厉害', '好棒', '真棒', '完美', '优秀', '唱得好', '天才', '有天赋', '很强'), ('开心', 34)),
    (('我做到了', '我赢了', '我成功了', '我能行', '我进步了', '第一名'), ('自信', 27)),
    (('可爱', '好看', '漂亮'), ('害羞', 26)),
    (('辛苦了', '注意休息', '好好休息', '保重', '别累着', '早点睡', '吃了吗', '照顾好自己'), ('温暖', 22)),
    (('谢谢', '谢谢你', '感谢'), ('温暖', 16)),
    (('没关系', '没事了', '放心', '解决了', '别担心'), ('安心', 21)),
    (('明天', '约好', '新曲', '演出', '一起去看', '到时候'), ('期待', 17)),
    (('惊喜', '礼物', '中奖', '好消息'), ('惊喜', 24)),
    (('感动', '哭了', '真心话', '一直记得'), ('感动', 23)),
    (('不配', '我不行', '比不上', '太差劲', '没资格', '我是废物', '拖后腿', '没出息', '没天赋'), ('自卑', 35)),
    (('好累', '累死', '不舒服', '生病', '失眠', '没睡好', '加班', '熬夜'), ('担忧', 19)),
]


DEFAULT_POS_RES = '开心=5,温暖=3,期待=4,安心=3,自信=4,惊喜=5,感动=5'
DEFAULT_NEG_RES = '生气=5,难过=4,害怕=4,厌恶=5,委屈=4,紧张=3,失望=4,嫉妒=4,自卑=4'


def _parse_resistance_map(val, default=''):
    """解析 "开心=5,温暖=3" → {'开心': 5, '温暖': 3}（缺省项用 5）"""
    try:
        m = {}
        for part in str(val or '').split(','):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                if k.strip() and str(v).strip().isdigit():
                    m[k.strip()] = int(v.strip())
        if not m and default:
            return _parse_resistance_map(default)
        return m
    except Exception:
        if not default:
            return {}
        try:
            return _parse_resistance_map(default)
        except Exception:
            return {}

def _resistance_total(state: dict, emotion: str, res_pos_map: dict, res_neg_map: dict,
                      pos_thr: list, neg_thr: list, neu_thr: list | None = None,
                      et_map: dict | None = None) -> int:
    """反向冲击减值 = Σ(对面每种情绪阶段 × 该情绪每阶段减值)——阶段单算、减值累加"""
    group = _group_of(emotion)
    opp = 'neg' if group == 'pos' else 'pos'
    res_map = res_pos_map if opp == 'pos' else res_neg_map
    total = 0
    for e, sc in state.get(opp, {}).items():
        thr = _thr_for(e, pos_thr, neg_thr, neu_thr or pos_thr, et_map)
        stage = _calc_stage(sc, thr)
        total += stage * res_map.get(e, 5)
    return total


def _fallback_judge(text: str) -> list:
    hits = []
    for keywords, (emotion, score) in RULE_FALLBACK:
        for kw in keywords:
            if kw in text:
                hits.append((emotion, score))
                break
    return hits


# ===== 配置解析 =====
def _parse_thresholds(val, default='20,40,60,80'):
    try:
        items = [int(x.strip()) for x in str(val or '').split(',') if str(x).strip().isdigit()]
        return items if items else [int(x) for x in default.split(',')]
    except Exception:
        return [20, 40, 60, 80]


def _calc_stage(total: float, thresholds: list) -> int:
    """情绪分 → 阶段：低于第一个阈值 = 0（有情绪但未达阶段，只算"有点"）；
    达到第一个阈值 = 阶段1；阶段数 = 阈值个数（默认 4 个阈值 → 0-4 阶段）"""
    if total <= 0 or not thresholds:
        return 0
    stage = 0
    for t in thresholds:
        if total >= t:
            stage += 1
    return stage


def _state_total(state: dict) -> float:
    return sum(state.get('pos', {}).values()) - sum(state.get('neg', {}).values())


def _dominant(state: dict, group: str) -> tuple:
    items = state.get(group, {})
    if not items:
        return ('', 0)
    e = max(items, key=items.get)
    return (e, items[e])


# ===== 解析防护 =====
def _safe_score(val, default=None):
    """LLM 返回的分数转 int：支持 int/float/数字字符串；解析失败返回 default"""
    try:
        if isinstance(val, bool):
            return default
        if isinstance(val, (int, float)):
            return int(val)
        s = str(val or '').strip()
        if not s:
            return default
        if s.lstrip('+-').isdigit():
            return int(s)
        return int(float(s))
    except Exception:
        return default


def _safe_int(val, default: int) -> int:
    """配置值转 int，解析失败返回 default"""
    try:
        return int(val)
    except Exception:
        try:
            return int(float(val))
        except Exception:
            return default


def _safe_float(val, default: float) -> float:
    """配置值转 float，解析失败返回 default"""
    try:
        return float(val)
    except Exception:
        return default


# ===== LLM 判断 =====
def _llm_call(base: str, model: str, key: str, sys_prompt: str, user_text: str) -> dict | None:
    try:
        body = json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': user_text},
            ],
            'max_tokens': 700,
            'temperature': 0.3,
        }).encode()
        req = urllib.request.Request(
            base,
            data=body,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}',
                     'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'},
            method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


async def llm_judge_core(text: str, prev_text: str, hint: str, base: str, model: str, key: str) -> tuple | None:
    """LLM 语义判断：返回 [(情绪, 强度分 0-100, is_event)] 或 None"""
    try:
        if not key:
            return None
        sys_prompt = (
            '你是一个角色的情绪判断器。给你一条用户刚发来的消息，判断这条消息会让角色产生什么情绪。\n'
            '可选情绪：开心、温暖、期待、安心、自信、惊喜、感动（正面）；生气、难过、害怕、厌恶、委屈、失望、嫉妒、自卑（负面）；害羞、担忧、紧张（可与其他情绪并存：又羞又恼=害羞+生气，被喜欢的人夸=开心+害羞）\n'
            '角色对说话的人是在意的亲近对象，会自然地关心 ta 的状态：对方说累/不舒服/难过时，角色是担忧而不是自己难过\n'
            '对方遭遇不顺（被骂、被欺负、被凶、生病、工作受挫、家里出事）时，角色会担忧、心疼；如果错在第三方（如被领导骂），角色还会替对方生气（共情生气）——这不是无情绪的时候\n'
            '对方说越界/骚扰内容（陪睡、威胁、强迫、开黄腔、露骨要求、过分亲密要求如让我抱抱/身上好软）时，角色会害怕、厌恶、生气，而不是害羞或温暖——这不是撒娇，是冒犯\n'
            '对方用亲密称呼叫角色（妈妈、老婆、亲爱的等）时，角色会害羞+抗拒，想纠正对方，不会默认接受这个称呼；软磨硬泡可以勉强不纠正，但跟着就提亲亲抱抱喂食等要求时，角色会生气/厌恶地拒绝，而不是配合\n'
            '对方反复索要感情确认（你可以说爱我吗、你爱我吗、你是不是不爱我了）时，角色会害羞、紧张、不知怎么回应，倾向回避，而不是开心温暖；已经回应过的话，不会因为对方反复问就反复哄着说\n'
            '对方分享/转述的内容（歌词、文章、小说、视频台词、截图文字、别人说的话、题目等）不是对方对角色本人的发言：可以因内容本身产生情绪（如喜欢这首歌=开心/感动），但不要因此对对方本人产生厌恶、生气、委屈、失望等负面情绪；纯信息文本（歌名、作词作曲、日期、链接、清单等）默认无情绪，返回空 emotions；\n'
            '对方道歉/认错/安抚（对不起、抱歉、我错了、别生气、没事了、别难过）时，你会被哄到、松一口气（温暖/安心），怒气会消，而不是更委屈或更生气；\n'
            f'当前情绪状态：{hint or "平静"}\n'
            f'之前对方说的话：{prev_text or "（无，刚开始对话）"}\n'
            '要求：只根据这条消息本身判断，不要脑补背景；'
            'score 是该情绪对角色造成的影响强度，0-100 连续数值：日常普通闲聊通常只有 0-15（轻微，常态）；16-35=有点明显（夸奖、关心、聊得来等日常小互动，仍属日常）；36-65=明显（事件级：对方状态明显不好、小冲突、小惊喜）；66-100=强烈（重大事件：表白、被凶、争吵、恶意辱骂、坏消息/好消息、离别等）；'
            '注意：夸奖（包括夸唱歌、夸长相、说喜欢听你唱歌）、日常关心、日常邀约、说加油鼓励，都属于日常互动，score 不应超过 35，is_event 一律为 false；'
            'score 必须是自然连续的整数（如 7、18、37、52、86），不要取整到整十或整五；'
            'is_event: 这条消息是否属于重大事件——仅限表白、争吵、被凶、恶意辱骂、道歉、坏消息/好消息、获奖、生病、离别等明确事件；日常夸奖、日常关心、日常闲聊一律为 false；'
            '没有明显情绪变化就返回空 emotions；只输出 JSON，格式：{"emotions": [{"emotion": "情绪名", "score": 0-100, "is_event": true/false}], "reason": "一句话原因"}（一句话可以同时带来多个情绪，有多少输出多少，每个单独打分，不要限制数量）'
        )
        # 重试一次：LLM 偶发返回空内容/超时（长消息更明显），不让漏判直接落到兜底
        content = ''
        for _attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(_llm_call, base, model, key, sys_prompt, f'消息：{text[:200]}'),
                    timeout=15)
                if resp:
                    content = (resp.get('choices') or [{}])[0].get('message', {}).get('content', '') or ''
                if content.strip():
                    break
            except Exception:
                continue
        if not content.strip():
            return None
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        emotions = data.get('emotions')
        if isinstance(emotions, list) and emotions:
            out = []
            for item in emotions:
                if not isinstance(item, dict):
                    continue
                emo = str(item.get('emotion', '') or '').strip()
                sc = _safe_score(item.get('score'))
                if sc is None:
                    continue
                if emo and (emo in POSITIVE or emo in NEGATIVE or emo in COMPLEX) and 1 <= sc <= 100:
                    out.append((emo, sc, bool(item.get('is_event'))))
            return out or None
        emo = str(data.get('emotion', '') or '').strip()
        sc = _safe_score(data.get('score'))
        if sc is not None and emo and (emo in POSITIVE or emo in NEGATIVE or emo in COMPLEX) and 1 <= sc <= 100:
            return [(emo, sc, bool(data.get('is_event')))]
    except Exception:
        return None
    return None


async def llm_topic_change_core(prev_text: str, text: str, base: str, model: str, key: str) -> bool:
    """专门判断这条消息是否与上一条完全无关（转移话题）"""
    try:
        if not key or not prev_text:
            return False
        sys_prompt = (
            '判断两句连续的话是否在讨论同一件事。\n'
            f'前一句：{prev_text[:100]}\n'
            f'这一句：{text[:100]}\n'
            '如果这一句突然切换到与前面完全无关的新话题（转移话题），输出 true；'
            '如果是接续、回应、解释、收尾（如"算了不说这个""先不说这个""其实我是想说"），输出 false。'
            '只输出 true 或 false，不要输出其他内容。'
        )
        resp = await asyncio.wait_for(
            asyncio.to_thread(_llm_call, base, model, key, sys_prompt, '请判断'),
            timeout=12)
        if not resp:
            return False
        content = (resp.get('choices') or [{}])[0].get('message', {}).get('content', '') or ''
        return 'true' in content.lower() and 'false' not in content.lower()
    except Exception:
        return False


# ===== 状态机 =====
def _decay_state(state: dict | None, now: float, decay_minutes: int, decay_points: int) -> dict | None:
    if not state:
        return None
    ts = float(state.get('ts', now))
    drops = int((now - ts) // (decay_minutes * 60))
    if drops <= 0:
        return state
    out = dict(state)
    for group in ('pos', 'neg', 'neu'):
        d = {}
        for e, sc in out.get(group, {}).items():
            # 高分段加速衰减：接近满格消得更快，避免长时间卡在满格
            pts = decay_points
            if sc >= 90:
                pts = decay_points * 3
            elif sc >= 70:
                pts = decay_points * 2
            ns = sc - drops * pts
            if ns > 0:
                d[e] = ns
        out[group] = d
    out['ts'] = ts + drops * decay_minutes * 60
    if not out['pos'] and not out['neg'] and not out['neu']:
        return None
    return out


def _unfreeze(state: dict) -> dict:
    """冻结分归还正池"""
    frozen = state.pop('frozen', {})
    frozen.pop('type', None)
    if frozen:
        pos = dict(state.get('pos') or {})
        for e, sc in frozen.items():
            pos[e] = min(100, pos.get(e, 0) + sc)
        state['pos'] = pos
    return state


def _check_unfreeze(state: dict, neu_thr: list, neg_thr: list) -> dict:
    """负面/担忧消散后自动解冻（问题翻篇，冻结恢复采纳）"""
    if not state.get('frozen'):
        return state
    ftype = state['frozen'].get('type', 'cold_water')
    if ftype == 'worry':
        if state.get('neu', {}).get('担忧', 0) < (neu_thr or [20])[0]:
            return _unfreeze(state)
    else:
        neg_total = sum(state.get('neg', {}).values())
        if neg_total < (neg_thr or [20])[0]:
            return _unfreeze(state)
    return state


def _apply_emotion(state: dict | None, emotion: str, score: int, source: str, now: float,
                   decay_minutes: int, decay_points: int, pos_thr: list, neg_thr: list,
                   resistance_enabled: bool, res_pos_map: dict, res_neg_map: dict,
                   bonus_map: dict | None = None, neu_thr: list | None = None,
                   conv_ratio: float = 0.7, cold_water_enabled: bool = True,
                   impact_graph: dict | None = None, et_map: dict | None = None,
                   min_neg_residue: int = 5,
                   damp_curve: dict | None = None, inject_cap: dict | None = None,
                   is_event: bool = False, cold_water_min: int = 25) -> dict | None:
    state = _decay_state(state, now, decay_minutes, decay_points)
    if state is None:
        state = {'pos': {}, 'neg': {}, 'neu': {}, 'frozen': {}, 'ts': now, 'events': [], 'last_msg_ts': now, 'settle_hist': []}
    else:
        state = _check_unfreeze(state, neu_thr, neg_thr)
    group = _group_of(emotion)

    # 情绪易感加值：角色对特定情绪更敏感（比如容易受伤的角色对难过/委屈有额外加成）
    if score > 0 and bonus_map:
        score = min(100, score + bonus_map.get(emotion, 0))
    # 多段阻尼：每情绪独立曲线（易感加值先计入再压缩；普通闲聊天然到不了高分段）
    if score > 0:
        score = _damp_score(score, _damp_curve_for(emotion, damp_curve))
    # 普通对话单情绪注入上限：非事件消息每句该情绪最多注入上限分（事件才有额外值）
    if score > 0 and not is_event:
        score = min(score, _inject_cap_for(emotion, inject_cap))

    if group != 'neu':
        # 泼冷水：此刻带着正面情绪（正池非空——包括害羞等复合情绪在场的正面时刻），
        # 突然的负面打击（落差感，负面迅速生成）
        cold_water = False
        if cold_water_enabled and group == 'neg' and state.get('pos') and score >= cold_water_min:
            cold_water = True
        state = dict(state)
        state['events'] = list(state.get('events', []))
        state['events'].append({'e': emotion, 's': score, 't': source})
        state['events'] = state['events'][-8:]
        if cold_water:
            # 泼冷水：负面全额进负池（不抗性、不先相抵），正面被扣相应分，
            # 剩余正面冻结（保留但不采纳：不参与抗性/主导/表达），直到道歉解冻
            neg_pool = state.get('neg', {})
            neg_pool[emotion] = min(100, neg_pool.get(emotion, 0) + score)
            state['neg'] = neg_pool
            pos_pool = state.get('pos', {})
            remain = score
            for e in sorted(pos_pool, key=pos_pool.get, reverse=True):
                if remain <= 0:
                    break
                take = min(pos_pool[e], remain)
                pos_pool[e] = pos_pool[e] - take
                remain -= take
                if pos_pool[e] <= 0:
                    del pos_pool[e]
            state['pos'] = pos_pool
            if pos_pool:
                # 剩余正面全部冻结（泼冷水源：负面消散前不采纳）
                frozen = dict(state.get('frozen') or {})
                frozen.pop('type', None)
                for e, sc in pos_pool.items():
                    frozen[e] = frozen.get(e, 0) + sc
                frozen['type'] = 'cold_water'
                state['frozen'] = frozen
                state['pos'] = {}
            return state
        # 冻结期：新负面先扣冻结池（问题没解决前接着扣），扣完才走正常流程
        if group == 'neg' and state.get('frozen'):
            frozen = dict(state.get('frozen') or {})
            remain = score
            for e in sorted((k for k in frozen if k != 'type'), key=frozen.get, reverse=True):
                if remain <= 0:
                    break
                take = min(frozen[e], remain)
                frozen[e] = frozen[e] - take
                remain -= take
                if frozen[e] <= 0:
                    del frozen[e]
            if any(k != 'type' for k in frozen):
                state['frozen'] = frozen
            else:
                state.pop('frozen', None)
            if remain <= 0:
                return state
            score = remain
        # 抗性：对面每种情绪按自己的阶段减值，累加（情绪越高减得越多，可叠加）
        if resistance_enabled and score > 0:
            total = _resistance_total(state, emotion, res_pos_map, res_neg_map, pos_thr, neg_thr, neu_thr, et_map)
            score = max(0, score - total)
        if score <= 0:
            return state
        # 图谱扩散扣减：总伤害 = score，按权重加权分给命中的目标情绪（互斥对象扣最多，其他被波及）
        # 图谱命中时：剩余进本池（图谱外情绪不受波及）；图谱全空才走旧池级相抵
        if impact_graph and group in ('pos', 'neg'):
            targets = [(t, r) for t, r in impact_graph.get(emotion, [])
                       if state.get(_group_of(t), {}).get(t, 0) > 0]
            if targets:
                total_ratio = sum(r for _, r in targets)
                remain = score
                for t, r in targets:
                    if remain <= 0:
                        break
                    pool = state.get(_group_of(t), {})
                    quota = score * r / total_ratio
                    take = min(pool[t], quota, remain)
                    if take > 0:
                        pool[t] = pool[t] - take
                        remain -= take
                        if pool[t] <= 0:
                            del pool[t]
                        state[_group_of(t)] = pool
                if remain > 0:
                    # 图谱内扣不完的剩余进本池（情绪自己存在，不波及图谱外）
                    cur = state.get(group, {})
                    cur[emotion] = min(100, cur.get(emotion, 0) + remain)
                    state[group] = cur
                elif group == 'neg' and min_neg_residue > 0:
                    # 负面被完全抵消：最低残留（被伤了总归有点感觉）
                    cur = state.get(group, {})
                    if cur.get(emotion, 0) < min_neg_residue:
                        cur[emotion] = min_neg_residue
                        state[group] = cur
                return state
        # 异类相抵：图谱全空时的兜底（从分最高的情绪开始）
        opposite = 'neg' if group == 'pos' else 'pos'
        opp_items = state.get(opposite, {})
        remain = score
        if opp_items:
            for e in sorted(opp_items, key=opp_items.get, reverse=True):
                if remain <= 0:
                    break
                cur_v = opp_items[e]
                take = min(cur_v, remain)
                opp_items[e] = cur_v - take
                remain -= take
                if opp_items[e] <= 0:
                    del opp_items[e]
            state[opposite] = {k: v for k, v in opp_items.items() if v > 0}
        if remain <= 0:
            if group == 'neg' and min_neg_residue > 0:
                cur = state.get(group, {})
                if cur.get(emotion, 0) < min_neg_residue:
                    cur[emotion] = min_neg_residue
                    state[group] = cur
            return state
        score = remain
    else:
        # 复合情绪：独立池，不与正负相抵、不参与抗性，可与任何情绪并存
        state = dict(state)
        state['events'] = list(state.get('events', []))
        state['events'].append({'e': emotion, 's': score, 't': source})
        state['events'] = state['events'][-8:]

    # 同类叠加：单情绪 cap 100（池总和不设限，阶段有 5 级封顶兜着）
    cur = state[group]
    cur[emotion] = min(100, cur.get(emotion, 0) + score)
    state[group] = cur

    # ===== 复合情绪联动（图谱驱动） =====
    # 担忧/紧张达到阶段后，按影响图谱处理目标情绪：
    #   安心 → 对应削减（互斥）；其他正池目标（开心/期待/自信…）→ 部分转温暖（可用）+ 剩余冻结（解决后恢复）
    if emotion in ('担忧', '紧张') and state['neu'].get(emotion, 0) >= _thr_for(emotion, pos_thr, neg_thr, neu_thr or [20], et_map)[0]:
        pos = state.get('pos', {})
        targets = (impact_graph or {}).get(emotion, [])
        changed = False
        for t, r in targets:
            if t == '安心' and pos.get('安心', 0) > 0:
                take = min(score, pos['安心'])
                pos['安心'] = pos['安心'] - take
                if pos['安心'] <= 0:
                    del pos['安心']
                changed = True
            elif t in POSITIVE and pos.get(t, 0) > 0:
                h = pos.pop(t)
                warm = int(h * conv_ratio)
                frozen_part = h - warm
                if warm > 0:
                    pos['温暖'] = min(100, pos.get('温暖', 0) + warm)
                if frozen_part > 0:
                    frozen = dict(state.get('frozen') or {})
                    frozen.pop('type', None)
                    frozen[t] = frozen.get(t, 0) + frozen_part
                    frozen['type'] = 'worry'
                    state['frozen'] = frozen
                changed = True
        if changed:
            state['pos'] = pos
    elif emotion in POSITIVE and state.get('neu', {}):
        # 担忧/紧张未消时，新来的正池情绪如果在该复合情绪的影响图谱里 → 部分转温暖、剩余冻结
        for ce in ('担忧', '紧张'):
            if state['neu'].get(ce, 0) >= _thr_for(ce, pos_thr, neg_thr, neu_thr or [20], et_map)[0]:
                targets = (impact_graph or {}).get(ce, [])
                if any(t == emotion for t, _ in targets):
                    cur = state.get('pos', {})
                    if cur.get(emotion, 0) > 0:
                        h = cur.pop(emotion)
                        warm = int(h * conv_ratio)
                        frozen_part = h - warm
                        if warm > 0:
                            cur['温暖'] = min(100, cur.get('温暖', 0) + warm)
                        if frozen_part > 0:
                            frozen = dict(state.get('frozen') or {})
                            frozen.pop('type', None)
                            frozen[emotion] = frozen.get(emotion, 0) + frozen_part
                            frozen['type'] = 'worry'
                            state['frozen'] = frozen
                        state['pos'] = cur
                    break

    # 安心与担忧/紧张对应（双向）：安心恢复时削减担忧/紧张（问题解决），并检查解冻
    if emotion == '安心' and state.get('neu', {}):
        neu = state['neu']
        t1 = min(score, neu.get('担忧', 0))
        if t1 > 0:
            neu['担忧'] = neu['担忧'] - t1
            if neu['担忧'] <= 0:
                del neu['担忧']
        t2 = min(score, neu.get('紧张', 0))
        if t2 > 0:
            neu['紧张'] = neu['紧张'] - t2
            if neu['紧张'] <= 0:
                del neu['紧张']
        state['neu'] = neu
        if state.get('frozen'):
            state = _check_unfreeze(state, neu_thr, neg_thr)
    return state


# 表白/亲密索取词：这类消息是索取感情/亲密，不是“对方缓和”，不触发强制安慰（宁宁对索取是害羞回避，不是被哄到）
INTIMATE_PULL_WORDS = ['爱我', '喜欢你', '爱不爱', '亲亲', '抱抱', '陪我睡', '叫我妈妈', '叫我老婆',
                       '老婆', '亲爱的', '喂我', '亲我', '抱我', '说你爱我']

VIOLATION_HINTS = {
    'light': '对方刚才对你说了越界的话（轻度）。你有点不自在、不太想配合，回应要带出距离感。',
    'mid': '对方刚才对你说了越界的话（中度）。你很生气、厌恶，不想配合对方，回应要明确拒绝、划清界限。',
    'severe': '对方刚才严重越界冒犯了你。你非常生气、觉得被冒犯，想当场怼回去，不给好脸色。',
    'bottom_line': '对方刚才触碰了你的底线。你又难过又害怕，不想说话，只想躲开，回应极简冷淡。',
}


def _violation_hint(state: dict | None, now: float, window: int = 300) -> str:
    """最近越界事件的生成侧提示（注入文本最高优先级段）。window 秒内有效。"""
    if not isinstance(state, dict):
        return ''
    v = state.get('violation')
    if not isinstance(v, dict):
        return ''
    try:
        vt = float(v.get('ts', 0) or 0)
    except Exception:
        return ''
    if vt <= 0 or now - vt > window:
        return ''
    level = str(v.get('level') or 'mid')
    base = VIOLATION_HINTS.get(level)
    if not base:
        return ''
    emos = v.get('emotions') or []
    if emos:
        base += f'（你现在的情绪：{"、".join(str(e) for e in emos)}）'
    return base


def _fmt_pools(state: dict | None) -> str:
    """日志用：情绪池紧凑摘要（不含原文）"""
    if not state:
        return '空'
    parts = []
    for grp in ('pos', 'neg', 'neu'):
        d = state.get(grp) or {}
        if d:
            items = ','.join(f'{k}:{int(v)}' for k, v in sorted(d.items(), key=lambda x: -x[1]))
            parts.append(f'{grp}[{items}]')
    return '; '.join(parts) if parts else '空'


def _format_injection(state: dict | None, pos_thr: list, neg_thr: list, neu_thr: list,
                      et_map: dict | None = None, now: float | None = None) -> str:
    if not state:
        return ''
    now = time.time() if now is None else now
    pos = state.get('pos', {})
    neg = state.get('neg', {})
    events = {e.get('e'): e.get('t', '') for e in state.get('events', []) if e.get('t')}

    neu = state.get('neu', {})
    v_hint = _violation_hint(state, now)
    if not pos and not neg and not neu:
        return f'情绪状态补充：【越界反应】{v_hint}' if v_hint else ''

    def fmt_item(e, sc, sign, thr):
        thr = _thr_for(e, pos_thr, neg_thr, neu_thr, et_map)
        stage = _calc_stage(sc, thr)
        src = events.get(e, '')
        if stage == 0:
            s = f'「有点{e}」（{sign}{int(sc)}分'
        else:
            s = f'「{e}·第{stage}阶段」（{sign}{int(sc)}分'
        if src:
            s += f'，因为ta说了“{src[:25]}”'
        s += '）'
        return s

    # 主导情绪优先级：负面 > 担忧/紧张（偏负复合） > 害羞 > 正面
    def _pick_dominant():
        if neg:
            e = max(neg, key=neg.get)
            return ('neg', e, neg[e])
        for e in ('担忧', '紧张'):
            if neu.get(e, 0) > 0:
                return ('neu', e, neu[e])
        if neu:
            e = max(neu, key=neu.get)
            return ('neu', e, neu[e])
        if pos:
            e = max(pos, key=pos.get)
            return ('pos', e, pos[e])
        return None

    dom = _pick_dominant()
    if not dom and not v_hint:
        return ''
    parts = []
    # 越界反应段优先级最高：boundary 判定的越界 > LLM 情绪判断
    if v_hint:
        parts.append(f'【越界反应】{v_hint}')
    if dom:
        dom_group, dom_e, dom_sc = dom
        dom_sign = '+' if dom_group == 'pos' else '-' if dom_group == 'neg' else ''
        parts.append(f'你此刻主要是{fmt_item(dom_e, dom_sc, dom_sign, None)}')

        # 其余情绪作为底色
        others = []
        for e, sc in sorted(pos.items(), key=lambda x: -x[1]):
            if dom_group == 'pos' and e == dom_e:
                continue
            others.append(fmt_item(e, sc, '+', pos_thr))
        for e, sc in sorted(neu.items(), key=lambda x: -x[1]):
            if dom_group == 'neu' and e == dom_e:
                continue
            others.append(fmt_item(e, sc, '', neu_thr))
        for e, sc in sorted(neg.items(), key=lambda x: -x[1]):
            if dom_group == 'neg' and e == dom_e:
                continue
            others.append(fmt_item(e, sc, '-', neg_thr))
        if others:
            parts.append('同时带着' + '、'.join(others))
    # 沉淀历史
    hist = state.get('settle_hist') or []
    if hist:
        last = hist[-1]
        parts.append(f'（最近一次情绪沉淀：{last.get("ts_text", "")}因为“{str(last.get("source", ""))[:20]}”你{last.get("label", "")}）')
    parts.append('这些情绪还有余波，不会马上消失。你的回应会自然地带着它们——是什么情绪就怎么反应，不用压着。')
    return '情绪状态补充：' + ' '.join(parts)


class TempEmotionPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}
        data_dir = get_astrbot_plugin_data_path()
        os.makedirs(data_dir, exist_ok=True)
        self.state_path = os.path.join(data_dir, 'temp_emotion_state.json')
        self.diary_path = os.path.join(data_dir, 'emotion_diary.json')
        self._state = self._load_state()
        self._diary = self._load_diary()
        self._lock = asyncio.Lock()
        logger.info('[TempEmotion] 临时情绪插件已启动（分数+阶段制）')

    def _cfg(self, key, default=None):
        try:
            c = self.config
            if isinstance(c, dict):
                if key in c and c[key] is not None:
                    return c[key]
                # AstrBot 新版插件配置按 _conf_schema 分组嵌套（如 "基础开关": {...}），
                # 直接键取不到时扫描各分组，兼容两种存储形态。
                for group in c.values():
                    if isinstance(group, dict) and key in group and group[key] is not None:
                        return group[key]
                return default
            v = getattr(c, key, None)
            return default if v is None else v
        except Exception:
            return default

    def _load_state(self) -> dict:
        try:
            if os.path.isfile(self.state_path):
                with open(self.state_path, encoding='utf-8') as f:
                    d = json.load(f)
                    return d if isinstance(d, dict) else {}
        except Exception:
            pass
        return {}

    def _load_diary(self) -> list:
        try:
            if os.path.isfile(self.diary_path):
                with open(self.diary_path, encoding='utf-8') as f:
                    d = json.load(f)
                    return d if isinstance(d, list) else []
        except Exception:
            pass
        return []

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f'[TempEmotion] 保存状态失败: {e}')

    def _save_diary(self) -> None:
        try:
            tmp = self.diary_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._diary[-200:], f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.diary_path)
        except Exception as e:
            logger.warning(f'[TempEmotion] 保存日记失败: {e}')

    def _decay_minutes(self) -> int:
        try:
            return max(1, int(self._cfg('decay_minutes', 10) or 10))
        except Exception:
            return 10

    def _decay_points(self) -> int:
        try:
            return max(1, int(self._cfg('decay_points', 5) or 5))
        except Exception:
            return 5

    def _pos_thr(self):
        return _parse_thresholds(self._cfg('positive_stage_thresholds', '20,40,60,80'))

    def _neg_thr(self):
        return _parse_thresholds(self._cfg('negative_stage_thresholds', '20,40,60,80'))

    def _neu_thr(self):
        return _parse_thresholds(self._cfg('neutral_stage_thresholds', '20,40,60,80'))

    def _et_map(self):
        return _parse_emotion_thresholds(self._cfg('emotion_stage_thresholds', ''))

    def _damp_curve(self):
        return _parse_damp_curve(self._cfg('emotion_damp_curve', ''))

    def _inject_cap(self):
        return _parse_inject_cap_map(self._cfg('emotion_inject_cap', ''))

    def _state_hint(self, user_id: str) -> str:
        st = self._state.get(user_id)
        if not st:
            return '平静'
        etm = self._et_map()
        parts = []
        for e, sc in st.get('pos', {}).items():
            stage = _calc_stage(sc, _thr_for(e, self._pos_thr(), self._neg_thr(), self._neu_thr(), etm))
            parts.append(f'{e}·第{stage}阶段+{int(sc)}' if stage else f'有点{e}+{int(sc)}')
        for e, sc in st.get('neg', {}).items():
            stage = _calc_stage(sc, _thr_for(e, self._pos_thr(), self._neg_thr(), self._neu_thr(), etm))
            parts.append(f'{e}·第{stage}阶段-{int(sc)}' if stage else f'有点{e}-{int(sc)}')
        for e, sc in st.get('neu', {}).items():
            stage = _calc_stage(sc, _thr_for(e, self._pos_thr(), self._neg_thr(), self._neu_thr(), etm))
            parts.append(f'{e}·第{stage}阶段{int(sc)}' if stage else f'有点{e}{int(sc)}')
        return '、'.join(parts) if parts else '平静'

    def _maybe_settle(self, state: dict, now: float) -> tuple:
        """话头结束沉淀：池分达阈值 → 写日记 + 清池"""
        if not self._cfg('settle_enabled', True):
            return state, None
        idle_min = _safe_int(self._cfg('settle_idle_minutes', 20) or 20, 20)
        last_ts = _safe_float(state.get('last_msg_ts', now), now)
        if now - last_ts < idle_min * 60:
            return state, None
        pos_tot = sum(state.get('pos', {}).values())
        neg_tot = sum(state.get('neg', {}).values())
        p_thr = _safe_int(self._cfg('settle_positive_threshold', 60) or 60, 60)
        n_thr = _safe_int(self._cfg('settle_negative_threshold', 60) or 60, 60)
        events = []
        now_struct = time.localtime(now)
        ts_text = time.strftime('%m-%d %H:%M', now_struct)
        if pos_tot >= p_thr:
            emo, sc = _dominant(state, 'pos')
            src = (state.get('events') or [{}])[-1].get('t', '') if state.get('events') else ''
            ev = {'type': 'positive', 'score': int(pos_tot), 'dominant': emo, 'source': src[:60],
                  'ts': now, 'ts_text': ts_text, 'label': f'{emo}得很开心'}
            self._diary.append(ev)
            events.append(ev)
        if neg_tot >= n_thr:
            emo, sc = _dominant(state, 'neg')
            src = (state.get('events') or [{}])[-1].get('t', '') if state.get('events') else ''
            ev = {'type': 'negative', 'score': int(neg_tot), 'dominant': emo, 'source': src[:60],
                  'ts': now, 'ts_text': ts_text, 'label': f'被{emo}影响很深'}
            self._diary.append(ev)
            events.append(ev)
        if events:
            self._save_diary()
            hist = list(state.get('settle_hist') or [])
            hist.append(events[-1])
            state = {'pos': {}, 'neg': {}, 'neu': {}, 'frozen': {}, 'ts': now,
                     'events': state.get('events', [])[-4:],
                     'last_msg_ts': now, 'settle_hist': hist[-3:]}
            logger.info(f'[TempEmotion] 情绪沉淀: {len(events)} 条事件写入日记')
            return state, events
        return state, None

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=1000)
    async def on_private_message(self, event: AstrMessageEvent):
        """收到私聊消息 → 理解情绪 → 更新状态（不拦截）"""
        try:
            if not self._cfg('enabled', True):
                return None
            user_id = str(event.get_sender_id() or '')
            if not user_id:
                return None
            text = str(event.get_message_str() or '').strip()
            now = time.time()
            # 状态读改写加锁，防止并发消息互相覆盖
            async with self._lock:
                state = self._state.get(user_id)

                # 话头结束检查（先结算再处理新消息）
                if state:
                    idle_min = _safe_int(self._cfg('settle_idle_minutes', 20) or 20, 20)
                    if now - _safe_float(state.get('last_msg_ts', now), now) >= idle_min * 60:
                        state, _ev = self._maybe_settle(state, now)
                        state.pop('apology_used', None)  # 话头结束：道歉"只认第一次"重置

                judged = None
                topic_change = False
                forced_anger = None
                forced_comfort = None
                violation_fresh = False
                if text:
                    prev_text = ''
                    if state:
                        evts = state.get('events') or []
                        if evts:
                            prev_text = str(evts[-1].get('t', '') or '')
                    # 道歉/安抚：只解冻泼冷水源（对方造成的，道歉=问题解决）；
                    # 担忧源的冻结不解冻（担忧不是对方惹的，道歉无法解除担忧，要靠担忧消散/安心恢复）
                    if state:
                        apology_words = ['对不起', '抱歉', '我错了', '是我的错', '听我说', '别生气',
                                         '原谅我', '是我不好', '道歉', '别不理我', '我改', '误会']
                        if any(w in text for w in apology_words):
                            # 1) 解冻泼冷水源（原逻辑）
                            if state.get('frozen') and state['frozen'].get('type') != 'worry':
                                frozen = state.pop('frozen', {})
                                pos_pool = dict(state.get('pos') or {})
                                for e, sc in frozen.items():
                                    if e == 'type':
                                        continue
                                    pos_pool[e] = min(100, pos_pool.get(e, 0) + sc)
                                state['pos'] = pos_pool
                            # 2) 按档位削减负面池：只认第一次道歉（话头结束重置）
                            #    总负面 <60 削 40%、60~89 削 30%、>=90 削 15%（特别生气=不完全原谅）
                            #    不清零：每项至少留 min_neg_residue 残留（被伤了总归有点感觉）
                            if not state.get('apology_used'):
                                neg_pool = dict(state.get('neg') or {})
                                if neg_pool:
                                    total = sum(neg_pool.values())
                                    ratio = 0.15 if total >= 90 else (0.30 if total >= 60 else 0.40)
                                    mnr = _safe_int(self._cfg('min_neg_residue', 5) or 5, 5)
                                    cut_neg = {}
                                    for e, sc in neg_pool.items():
                                        ns = int(sc * (1 - ratio))
                                        if ns > 0:
                                            cut_neg[e] = max(ns, min(mnr, sc))
                                        elif sc > 0 and mnr > 0:
                                            cut_neg[e] = min(mnr, sc)
                                    state['neg'] = cut_neg
                                    state['apology_used'] = True
                                    logger.info(f'[TempEmotion] 道歉削减负面: user={user_id}, 总量{total}->{sum(cut_neg.values())}, 档位={ratio}')
                    # 越界优先：boundary（priority 200000）已先行实时注入负面情绪+越界标记。
                    # 当前消息触发的越界不再跑 LLM 判断（越界判定 > 普通情绪判断，防被语境带偏）
                    if state and isinstance(state.get('violation'), dict):
                        try:
                            vt = float(state['violation'].get('ts', 0) or 0)
                            violation_fresh = vt > 0 and (now - vt) < 5
                        except Exception:
                            violation_fresh = False
                    if not violation_fresh and self._cfg('judge_llm_enabled', True):
                        judged = await self._llm_judge(text, prev_text, self._state_hint(user_id))
                    if not judged and not violation_fresh:
                        fb = _fallback_judge(text)
                        if fb:
                            judged = [(e, s, False) for e, s in fb]

                cfg = dict(
                    dm=self._decay_minutes(), dp=self._decay_points(),
                    pt=self._pos_thr(), nt=self._neg_thr(), nut=self._neu_thr(),
                    re=bool(self._cfg('resistance_enabled', True)),
                    rpm=_parse_resistance_map(self._cfg('resistance_pos_map', DEFAULT_POS_RES), DEFAULT_POS_RES),
                    rnm=_parse_resistance_map(self._cfg('resistance_neg_map', DEFAULT_NEG_RES), DEFAULT_NEG_RES),
                    bm=_parse_resistance_map(self._cfg('emotion_bonus_map', ''), ''),
                    cr=_safe_float(self._cfg('conversion_ratio', 70) or 70, 70) / 100.0,
                    cw=bool(self._cfg('cold_water_enabled', True)),
                    eg=_parse_impact_graph(self._cfg('impact_graph', '')),
                    etm=_parse_emotion_thresholds(self._cfg('emotion_stage_thresholds', '')),
                    mnr=_safe_int(self._cfg('min_neg_residue', 5) or 5, 5),
                    cw_min=_safe_int(self._cfg('cold_water_min_score', 25) or 25, 25),
                    dc=self._damp_curve(), ic=self._inject_cap(),
                )
                # 强制规则（apply 前判定，用上一条消息结束时的状态）：
                #   转移话题 = 情绪不同类（上一条主情绪 vs 当前主情绪）+ 话题不同步（词面无共享）
                #   被惹类负面（对方造成）→ 生气叠加；担忧类（非对方造成）→ 安慰
                if state and prev_text and judged:
                    trigger = ['生气', '难过', '委屈', '厌恶', '失望', '嫉妒', '自卑']
                    neg_trigger = sum(v for e, v in state.get('neg', {}).items() if e in trigger)
                    worry = state.get('neu', {}).get('担忧', 0)
                    if max(neg_trigger, worry) >= 20:
                        apology_words = ['对不起', '抱歉', '我错了', '是我的错', '听我说', '别生气',
                                         '原谅我', '是我不好', '道歉', '别不理我', '我改', '误会']
                        comfort_words = ['别灰心', '别难过', '别怕', '没事的', '没事了', '没关系', '你很好', '你有你的好',
                                         '我相信你', '加油', '别往心里去', '不是你的错', '别担心', '会好起来的']
                        topic_change = False
                        if not any(w in text for w in apology_words + comfort_words):
                            prev_emo = state.get('last_emo', '')
                            cur_emo = judged[0][0]
                            topic_change = _is_topic_change(prev_text, text, prev_emo or None, cur_emo)
                        if topic_change:
                            cur_grp = _group_of(cur_emo) if cur_emo else None
                            if cur_grp == 'neg' and neg_trigger >= 20:
                                # 对方还在输出负面（继续踩雷/攻击）且转移话题 → 叠加生气
                                forced_anger = ('生气', min(100, int(neg_trigger)))
                            elif worry >= 20 and not any(w in text for w in INTIMATE_PULL_WORDS):
                                # 对方在缓和/示弱/自己状态不好 → 安慰（表白/亲密索取不算缓和）
                                forced_comfort = ('温暖', 20)
                if judged:
                    logger.info('[TempEmotion] 判断: user=%s, %s', user_id, ' '.join(
                        f'{e}({s}){"事件" if ev else "日常"}' for e, s, ev in judged))
                    before = _fmt_pools(state)
                    for emo, sc, is_ev in judged:
                        state = _apply_emotion(state, emo, sc, text[:40], now, cfg['dm'], cfg['dp'],
                                               cfg['pt'], cfg['nt'], cfg['re'], cfg['rpm'], cfg['rnm'], cfg['bm'], cfg['nut'], cfg['cr'], cfg['cw'], cfg['eg'], cfg['etm'], cfg['mnr'],
                                               cfg['dc'], cfg['ic'], is_ev, cfg['cw_min'])
                    if state is not None:
                        state['last_emo'] = judged[0][0]
                    logger.info('[TempEmotion] 更新: user=%s, %s -> %s', user_id, before, _fmt_pools(state))
                elif violation_fresh:
                    # 越界情绪已由 boundary 实时注入，保留注入结果，不再叠加普通判断/衰减
                    pass
                else:
                    state = _decay_state(state, now, cfg['dm'], cfg['dp'])
                # 转移话题：强制情绪最后叠加（不被本次判断削减）
                if forced_anger:
                    before = _fmt_pools(state)
                    state = _apply_emotion(state, forced_anger[0], forced_anger[1], text[:40], now, cfg['dm'], cfg['dp'],
                                           cfg['pt'], cfg['nt'], cfg['re'], cfg['rpm'], cfg['rnm'], cfg['bm'], cfg['nut'], cfg['cr'], cfg['cw'], cfg['eg'], cfg['etm'], cfg['mnr'],
                                           cfg['dc'], cfg['ic'], True, cfg['cw_min'])
                    logger.info('[TempEmotion] 强制叠加(转题生气): user=%s, %s -> %s', user_id, before, _fmt_pools(state))
                if forced_comfort:
                    before = _fmt_pools(state)
                    state = _apply_emotion(state, forced_comfort[0], forced_comfort[1], text[:40], now, cfg['dm'], cfg['dp'],
                                           cfg['pt'], cfg['nt'], cfg['re'], cfg['rpm'], cfg['rnm'], cfg['bm'], cfg['nut'], cfg['cr'], cfg['cw'], cfg['eg'], cfg['etm'], cfg['mnr'],
                                           cfg['dc'], cfg['ic'], True, cfg['cw_min'])
                    logger.info('[TempEmotion] 强制叠加(转题安慰): user=%s, %s -> %s', user_id, before, _fmt_pools(state))
                if state is None:
                    self._state.pop(user_id, None)
                else:
                    state['last_msg_ts'] = now
                    self._state[user_id] = state
                self._save_state()
        except Exception as e:
            logger.warning(f'[TempEmotion] 更新情绪失败: {e}')
        return None

    async def _llm_judge(self, text: str, prev_text: str, hint: str) -> list | None:
        try:
            base = str(self._cfg('judge_llm_base', 'https://api.deepseek.com/chat/completions') or '')
            model = str(self._cfg('judge_llm_model', 'deepseek-v4-flash') or '')
            key = str(self._cfg('judge_llm_api_key', '') or '').strip()
            return await llm_judge_core(text, prev_text, hint, base, model, key)
        except Exception:
            return None
    async def inject_external(self, user_id: str, emotion: str, score: int,
                             source: str = 'external', is_event: bool = True) -> bool:
        """外部插件（如边界系统）实时注入情绪：越界等行为 → 负面情绪入池（联动双向）。
        事件级注入：走阻尼曲线但不吃普通对话上限；锁内读改写，与消息链路一致。"""
        try:
            if not user_id or not emotion or score <= 0:
                return False
            now = time.time()
            async with self._lock:
                state = self._state.get(user_id)
                cfg = dict(
                    dm=self._decay_minutes(), dp=self._decay_points(),
                    pt=self._pos_thr(), nt=self._neg_thr(), nut=self._neu_thr(),
                    re=bool(self._cfg('resistance_enabled', True)),
                    rpm=_parse_resistance_map(self._cfg('resistance_pos_map', DEFAULT_POS_RES), DEFAULT_POS_RES),
                    rnm=_parse_resistance_map(self._cfg('resistance_neg_map', DEFAULT_NEG_RES), DEFAULT_NEG_RES),
                    bm=_parse_resistance_map(self._cfg('emotion_bonus_map', ''), ''),
                    cr=_safe_float(self._cfg('conversion_ratio', 70) or 70, 70) / 100.0,
                    cw=bool(self._cfg('cold_water_enabled', True)),
                    eg=_parse_impact_graph(self._cfg('impact_graph', '')),
                    etm=_parse_emotion_thresholds(self._cfg('emotion_stage_thresholds', '')),
                    mnr=_safe_int(self._cfg('min_neg_residue', 5) or 5, 5),
                    cw_min=_safe_int(self._cfg('cold_water_min_score', 25) or 25, 25),
                    dc=self._damp_curve(), ic=self._inject_cap(),
                )
                before = _fmt_pools(state)
                state = _apply_emotion(state, emotion, score, str(source)[:40], now,
                                       cfg['dm'], cfg['dp'], cfg['pt'], cfg['nt'],
                                       cfg['re'], cfg['rpm'], cfg['rnm'], cfg['bm'], cfg['nut'],
                                       cfg['cr'], cfg['cw'], cfg['eg'], cfg['etm'], cfg['mnr'],
                                       cfg['dc'], cfg['ic'], is_event, cfg['cw_min'])
                if state is None:
                    self._state.pop(user_id, None)
                else:
                    # 越界标记：boundary 实时注入的负面情绪记入状态，
                    # 供生成侧（_format_injection）按最高优先级输出越界反应段
                    src_str = str(source)
                    # nene 侧插件名 nene_boundary；主号侧插件名 boundary_feedback（前缀 boundary_）
                    if src_str.startswith('nene_boundary_'):
                        _lvl = src_str.replace('nene_boundary_', '', 1)
                    elif src_str.startswith('boundary_'):
                        _lvl = src_str.replace('boundary_', '', 1)
                    else:
                        _lvl = None
                    if _lvl:
                        lvl = _lvl
                        v = dict(state.get('violation') or {})
                        v['level'] = lvl
                        v['ts'] = now
                        emos = list(v.get('emotions') or [])
                        if emotion not in emos:
                            emos.append(emotion)
                        v['emotions'] = emos
                        state['violation'] = v
                        logger.info('[TempEmotion] 越界注入: user=%s, level=%s, 情绪=%s+%d%s, %s -> %s, emotions=%s',
                                    user_id, lvl, emotion, score, '事件' if is_event else '日常', before, _fmt_pools(state), emos)
                    state['last_msg_ts'] = now
                    state['last_emo'] = emotion
                    self._state[user_id] = state
                self._save_state()
            return True
        except Exception as e:
            logger.warning(f'[TempEmotion] 外部注入失败: {e}')
            return False


    @filter.on_llm_request(priority=-10000)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """回复前注入临时情绪余波（垫底执行，在 private_companion 注入之后）"""
        try:
            if not self._cfg('enabled', True) or not self._cfg('inject_enabled', True):
                return
            if event.get_message_type() != MessageType.FRIEND_MESSAGE:
                return
            user_id = str(event.get_sender_id() or '')
            if not user_id:
                return
            async with self._lock:
                state = self._state.get(user_id)
            if not state:
                return
            injection = _format_injection(state, self._pos_thr(), self._neg_thr(), self._neu_thr(), self._et_map())
            if not injection:
                return
            if not hasattr(req, 'system_prompt'):
                return
            current = req.system_prompt or ''
            marker = '\n【临时情绪】'
            if marker in current:
                return
            req.system_prompt = f'{current}{marker}\n{injection}'.strip()
            logger.info('[TempEmotion] 注入成功: user=%s, %s', user_id, _fmt_pools(state))
        except Exception as e:
            logger.warning(f'[TempEmotion] 注入失败: {e}')

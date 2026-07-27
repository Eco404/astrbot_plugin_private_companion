from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any


_DECISION_VERSION = 1

_SELFIE_WORKFLOWS = {"selfie", "portrait", "自拍", "人像"}
_EDIT_WORKFLOWS = {"edit", "改图", "修图", "重绘", "p图"}
_DAILY_OUTFIT_PATTERN = re.compile(
    r"(?:今日穿搭|当天穿搭|日常穿搭|today'?s outfit|daily outfit)\s*[：:]",
    flags=re.I,
)
_OUTFIT_PATTERNS = (
    ("cosplay", r"(?<![a-z0-9])cos(?:play)?(?![a-z0-9])|角色扮演|扮成|女仆装|巫女服|魔法少女|表演服"),
    ("school_uniform", r"校服|学院制服|学生制服|school[\s_-]*uniform"),
    ("sleepwear", r"睡衣|睡裙|睡袍|睡眠服|nightgown|nightdress|pajama|pyjama|sleepwear|bedtime outfit"),
    ("swimwear", r"泳装|泳衣|比基尼|swimsuit|swimwear|bikini"),
    ("sportswear", r"运动服|健身服|瑜伽服|球衣|sportswear|activewear|gym wear|jersey"),
    ("formalwear", r"礼服|晚礼服|正装|燕尾服|西装|tuxedo|formalwear|formal attire|evening gown|\bsuit\b"),
    ("homewear", r"居家服|家居服|家常服|宅家服|homewear|loungewear"),
    ("daily_outfit", r"今日穿搭|当天穿搭|日常穿搭|today'?s outfit|daily outfit"),
)
_CATEGORY_PRESETS = {
    "sleepwear": "居家睡衣",
    "homewear": "居家服",
    "cosplay": "COS自拍",
    "school_uniform": "校服人像",
    "formalwear": "礼服人像",
    "swimwear": "泳装人像",
    "sportswear": "运动服人像",
    "daily_outfit": "日常穿搭",
    "custom_outfit": "日常穿搭",
}
_PRESET_CATEGORIES = {
    "COS自拍": "cosplay",
    "日常穿搭": "daily_outfit",
    "居家睡衣": "sleepwear",
    "居家服": "homewear",
    "校服人像": "school_uniform",
    "礼服人像": "formalwear",
    "泳装人像": "swimwear",
    "运动服人像": "sportswear",
}
_CATEGORY_LABELS = {
    "sleepwear": "sleepwear",
    "homewear": "comfortable homewear",
    "cosplay": "the explicitly requested cosplay costume",
    "school_uniform": "school uniform",
    "formalwear": "formalwear",
    "swimwear": "swimwear",
    "sportswear": "sportswear",
    "daily_outfit": "today's daily outfit",
    "reference_outfit": "the complete outfit shown in the selected reference",
    "custom_outfit": "the outfit described in the current request",
}

__all__ = [
    "PhotoWardrobeIntent",
    "PhotoWardrobeDecision",
    "analyze_photo_wardrobe",
    "resolve_photo_wardrobe_decision",
]


def _clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip() if limit > 0 else text


def _outfit_category_matches(value: Any) -> list[tuple[str, int, int, str]]:
    text = _clean_text(value, 10000).lower()
    matches: list[tuple[str, int, int, str]] = []
    for category, pattern in _OUTFIT_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.I):
            matches.append((category, match.start(), match.end(), match.group(0)))
    matches.sort(key=lambda item: (item[1], item[2]))
    return matches


def _preset_category(preset_name: Any) -> str:
    name = _clean_text(preset_name, 80)
    if not name:
        return ""
    matches = _outfit_category_matches(name)
    return _PRESET_CATEGORIES.get(name) or (matches[0][0] if matches else "")


def _negative_clause_content(clause: str) -> tuple[bool, str]:
    text = _clean_text(clause, 4000).strip(" ,.;；。，")
    if not text:
        return False, ""
    text = re.sub(
        r"^(?:user\s+request|requested\s+final\s+image|用户要求|画面要求)\s*[：:]\s*",
        "",
        text,
        flags=re.I,
    ).strip()
    prefix = re.compile(
        r"^(?:请)?(?:不要|别(?:再)?(?:穿|用|选)?|不想穿|不穿|不用|不是|无需|无须|避免|禁止|不许|不得|排除|拒绝|去掉|脱下|取消)\s*"
        r"|^(?:do\s+not|don't|not|avoid|without|no|exclude|skip|remove)\s+",
        flags=re.I,
    )
    match = prefix.match(text)
    if match:
        return True, text[match.end():].strip(" ,.;；。，")
    postfix = re.compile(
        r"\s*(?:不要(?:了)?|别穿|不穿|不用|算了|就算了|除外|排除|取消|not|no)\s*$",
        flags=re.I,
    )
    match = postfix.search(text)
    if match:
        return True, text[:match.start()].strip(" ,.;；。，")
    return False, text


def _semantic_prompt_parts(prompt_text: str) -> tuple[str, str]:
    prompt = str(prompt_text or "").strip()
    positive_match = re.search(
        r"positive\s+prompt\s*:\s*(.*?)(?=negative\s+prompt\s*:|$)",
        prompt,
        flags=re.I | re.S,
    )
    if positive_match:
        positive_raw = positive_match.group(1).strip()
        negative_match = re.search(r"negative\s+prompt\s*:\s*(.*)$", prompt, flags=re.I | re.S)
        negative_raw = negative_match.group(1).strip() if negative_match else ""
    else:
        positive_raw = prompt
        negative_raw = ""

    positive_parts: list[str] = []
    negative_parts: list[str] = []

    def add_clause(raw_clause: str) -> None:
        clause = _clean_text(raw_clause, 4000).strip(" ,.;；。，")
        if not clause:
            return
        is_negative, content = _negative_clause_content(clause)
        if is_negative:
            transition = re.search(
                r"(?:但|而|不过|可是)?(?:改穿|换成|换上|换为|改为|要穿|穿上|而要)"
                r"|\b(?:but|instead|and)\s+(?:wear|change\s+into|switch\s+to|put\s+on)\b",
                content,
                flags=re.I,
            )
            if transition and transition.start() > 0:
                excluded = content[:transition.start()].strip(" ,.;；。，")
                requested = content[transition.start():].strip(" ,.;；。，")
                if excluded:
                    negative_parts.append(excluded)
                if requested:
                    positive_parts.append(requested)
                return
            if content:
                negative_parts.append(content)
            return
        if content:
            positive_parts.append(content)

    for clause in re.split(r"(?:\r?\n+|[。；;，,]+|(?<=[.!?])\s+)", positive_raw):
        add_clause(clause)
    for clause in re.split(r"(?:\r?\n+|[。；;，,]+|(?<=[.!?])\s+)", negative_raw):
        cleaned = _clean_text(clause, 4000).strip(" ,.;；。，")
        if not cleaned:
            continue
        _, content = _negative_clause_content(cleaned)
        if content:
            negative_parts.append(content)
    return ", ".join(dict.fromkeys(positive_parts)), ", ".join(dict.fromkeys(negative_parts))


def _current_user_request_parts(prompt_text: str) -> tuple[str, str]:
    raw = str(prompt_text or "")
    positive_match = re.search(
        r"positive\s+prompt\s*:\s*(.*?)(?=negative\s+prompt\s*:|$)",
        raw,
        flags=re.I | re.S,
    )
    if positive_match:
        positive_raw = positive_match.group(1)
        negative_match = re.search(r"negative\s+prompt\s*:\s*(.*)$", raw, flags=re.I | re.S)
        negative_raw = negative_match.group(1) if negative_match else ""
    else:
        positive_raw = raw
        negative_raw = ""

    marker = re.search(
        r"(?:\buser\s+request|\brequested\s+final\s+image|【最终画面需求】)\s*[：:]\s*",
        positive_raw,
        flags=re.I,
    )
    if marker:
        positive_raw = positive_raw[marker.end():]
        positive_raw = re.split(
            r",\s*(?:visible face|preserve unchanged subjects|clear main subject)\b",
            positive_raw,
            maxsplit=1,
            flags=re.I,
        )[0]
    exclusion_marker = re.search(
        r"(?:explicit\s+(?:wardrobe\s+)?exclusions?|明确排除的服装)\s*[：:]\s*(.*)$",
        positive_raw,
        flags=re.I | re.S,
    )
    if exclusion_marker:
        negative_raw = f"{negative_raw}, {exclusion_marker.group(1)}".strip(" ,")
        positive_raw = positive_raw[:exclusion_marker.start()]

    positive_text, embedded_negative = _semantic_prompt_parts(positive_raw)
    _, explicit_negative = _semantic_prompt_parts(
        f"Positive prompt: requested image. Negative prompt: {negative_raw}" if negative_raw else ""
    )
    negative_text = ", ".join(
        part for part in (embedded_negative, explicit_negative) if str(part or "").strip()
    )
    return (
        _clean_text(positive_text.strip(" \t\r\n,.;；。\"'"), 1800),
        _clean_text(negative_text.strip(" \t\r\n,.;；。\"'"), 1200),
    )


def _contains_specific_outfit_text(value: Any) -> bool:
    return bool(
        re.search(
            r"连衣裙|裙子|短裙|长裙|吊带|衬衫|外套|夹克|西装|制服|汉服|旗袍|和服|洛丽塔|"
            r"裤(?:子)?|毛衣|卫衣|T恤|背心|上衣|套装|袜(?:子)?|鞋(?:子)?|"
            r"\b(?:dress|skirt|shirt|blouse|coat|jacket|suit|uniform|hoodie|sweater|pants|trousers|shorts|top)\b",
            str(value or ""),
            flags=re.I,
        )
    )


@dataclass(frozen=True, slots=True)
class PhotoWardrobeIntent:
    target_category: str = ""
    target_text: str = ""
    custom_outfit: bool = False
    change_requested: bool = False
    excluded_categories: tuple[str, ...] = ()
    exclusion_text: str = ""
    positive_text: str = ""
    requested_scene_preset: str = ""
    requested_preset_category: str = ""


@dataclass(frozen=True, slots=True)
class PhotoWardrobeDecision:
    decision_version: int = _DECISION_VERSION
    rule_id: str = "none"
    mode: str = "none"
    source: str = "none"
    category: str = ""
    lock_outfit: bool = False
    remove_daily_outfit_context: bool = False
    preset_name: str = ""
    authoritative_preset: str = ""
    selected_presets: tuple[str, ...] = ()
    reference_image_path: str = ""
    reference_id: str = ""
    reference_kind: str = ""
    reference_roles: tuple[str, ...] = ()
    effective_reference_roles: tuple[str, ...] = ()
    positive_instruction: str = ""
    negative_instruction: str = ""
    reason: str = ""
    excluded_categories: tuple[str, ...] = ()
    requested_outfit_text: str = ""
    base_prompt: str = ""
    scene_context: str = ""
    adjustments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision_version != _DECISION_VERSION:
            raise ValueError(f"unsupported wardrobe decision version: {self.decision_version}")
        if not _clean_text(self.rule_id, 80):
            raise ValueError("rule_id must not be empty")
        if self.lock_outfit and not _clean_text(self.category, 80):
            raise ValueError("locked wardrobe decision requires a category")
        if not set(self.effective_reference_roles).issubset(self.reference_roles):
            raise ValueError("effective reference roles must be a subset of reference roles")
        if len(set(self.selected_presets)) != len(self.selected_presets):
            raise ValueError("selected presets must be unique")
        non_daily_category = bool(self.category and self.category != "daily_outfit")
        if (self.remove_daily_outfit_context or non_daily_category) and _DAILY_OUTFIT_PATTERN.search(
            self.scene_context
        ):
            raise ValueError("conflicting daily outfit context was not removed")
        if (self.remove_daily_outfit_context or non_daily_category) and re.search(
            r"keep today's outfit and character appearance consistent",
            self.base_prompt,
            flags=re.I,
        ):
            raise ValueError("generated daily outfit continuity was not removed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_version": self.decision_version,
            "rule_id": self.rule_id,
            "mode": self.mode,
            "source": self.source,
            "category": self.category,
            "lock_outfit": self.lock_outfit,
            "remove_daily_outfit_context": self.remove_daily_outfit_context,
            "preset_name": self.preset_name,
            "authoritative_preset": self.authoritative_preset,
            "selected_presets": list(self.selected_presets),
            "reference_image_path": self.reference_image_path,
            "reference_id": self.reference_id,
            "reference_kind": self.reference_kind,
            "reference_roles": list(self.reference_roles),
            "effective_reference_roles": list(self.effective_reference_roles),
            "positive_instruction": self.positive_instruction,
            "negative_instruction": self.negative_instruction,
            "reason": self.reason,
            "excluded_categories": list(self.excluded_categories),
            "requested_outfit_text": self.requested_outfit_text,
            "base_prompt": self.base_prompt,
            "scene_context": self.scene_context,
            "adjustments": list(self.adjustments),
        }


def analyze_photo_wardrobe(
    prompt_text: str,
    requested_scene_preset: str = "",
) -> PhotoWardrobeIntent:
    positive_text, negative_text = _current_user_request_parts(prompt_text)
    positive_matches = _outfit_category_matches(positive_text)
    negative_matches = _outfit_category_matches(negative_text)
    target_category = positive_matches[-1][0] if positive_matches else ""
    excluded_categories = tuple(
        dict.fromkeys(category for category, *_ in negative_matches if category != target_category)
    )
    change_requested = bool(
        re.search(
            r"换(?:装|衣|成|上|为|一套|一身|件)|改穿|改成|穿上|脱下.+(?:换|穿)|"
            r"\b(?:change\s+into|switch\s+to|put\s+on|change\s+(?:the\s+)?outfit|wear\s+instead)\b",
            positive_text,
            flags=re.I,
        )
    )
    custom_outfit = bool(
        not target_category
        and (
            change_requested
            or _contains_specific_outfit_text(positive_text)
            or re.search(
                r"(?:穿|换|改).{0,12}(?:衣服|服装|衣着|穿搭|一套|一身|一件)"
                r"|\b(?:wear|wearing|change|switch).{0,24}(?:clothes|clothing|outfit|wardrobe)\b",
                positive_text,
                flags=re.I,
            )
        )
    )
    wardrobe_negative_parts = [
        part.strip()
        for part in re.split(r"[,，;；。]+", negative_text)
        if part.strip()
        and (
            _outfit_category_matches(part)
            or _contains_specific_outfit_text(part)
            or re.search(r"衣服|服装|衣着|穿搭|clothes|clothing|outfit|wardrobe", part, flags=re.I)
        )
    ]
    preset = _clean_text(requested_scene_preset, 80)
    return PhotoWardrobeIntent(
        target_category=target_category or ("custom_outfit" if custom_outfit else ""),
        target_text=_clean_text(positive_text, 360) if target_category or custom_outfit else "",
        custom_outfit=custom_outfit,
        change_requested=change_requested,
        excluded_categories=excluded_categories,
        exclusion_text=_clean_text(", ".join(dict.fromkeys(wardrobe_negative_parts)), 360),
        positive_text=_clean_text(positive_text, 1800),
        requested_scene_preset=preset,
        requested_preset_category=_preset_category(preset),
    )


def _scene_without_daily_outfit_details(scene_context: str) -> str:
    text = _clean_text(scene_context, 2400)
    outfit_label = r"(?:今日穿搭|当天穿搭|日常穿搭|today'?s outfit|daily outfit)"
    if not text or not re.search(rf"{outfit_label}\s*[：:]", text, flags=re.I):
        return text
    cleaned = re.sub(
        rf"(^|[；;,，])\s*{outfit_label}\s*[：:].*?(?=[；;,，]\s*(?:视觉话题|时间|状态|当前日程|日程|情绪|可分享碎片|当前位置|地点|位置|当前场景|场景|天气背景|天气|背景|最近自拍|发型|发色|瞳色|表情|风格)[：:]|$)",
        lambda match: match.group(1),
        text,
        flags=re.S | re.I,
    )
    cleaned = re.sub(r"[；;,，]{2,}", "；", cleaned).strip("；;,， ")
    return _clean_text(cleaned, 2400)


def _daily_outfit_details(scene_context: str) -> str:
    match = re.search(
        r"(?:今日穿搭|当天穿搭|日常穿搭|today'?s outfit|daily outfit)\s*[：:]\s*(.*?)"
        r"(?=(?:[；;,，]|\n)\s*(?:视觉话题|时间|状态|当前日程|日程|情绪|可分享碎片|当前位置|地点|位置|"
        r"当前场景|场景|天气背景|天气|背景|最近自拍|发型|发色|瞳色|表情|风格)\s*[：:]|$)",
        str(scene_context or ""),
        flags=re.I | re.S,
    )
    return _clean_text(match.group(1), 600) if match else ""


def _normalized_exclusion_phrases(exclusion_text: str) -> tuple[str, ...]:
    phrases: list[str] = []
    for raw_phrase in re.split(r"[,，;；]+", str(exclusion_text or "")):
        phrase = _clean_text(raw_phrase, 360).lower()
        phrase = re.sub(
            r"^(?:请)?(?:穿着?|换成|改成|换上|改穿)\s*"
            r"|^(?:please\s+)?(?:wear(?:ing)?|change\s+into|switch\s+to|put\s+on)\s+",
            "",
            phrase,
            flags=re.I,
        )
        phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.I)
        normalized = re.sub(r"[\W_]+", "", phrase, flags=re.UNICODE)
        if normalized:
            phrases.append(normalized)
    return tuple(dict.fromkeys(phrases))


def _daily_outfit_matches_custom_exclusion(scene_context: str, exclusion_text: str) -> bool:
    details = re.sub(r"[\W_]+", "", _daily_outfit_details(scene_context).lower(), flags=re.UNICODE)
    return bool(details) and any(
        phrase in details for phrase in _normalized_exclusion_phrases(exclusion_text)
    )


def _prompt_without_generated_daily_outfit_continuity(prompt_text: str) -> str:
    text = str(prompt_text or "")
    replacements = (
        (
            r"keep today's outfit and character appearance consistent with the reference image",
            "keep character identity and stable appearance consistent with the selected reference image",
        ),
        (
            r"keep today's outfit and character appearance consistent with available visual continuity",
            "keep character identity and stable appearance consistent with available visual continuity",
        ),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    visual_memory_pattern = re.compile(
        r"(visual continuity reference:\s*)(.*?)"
        r"(?=,\s*(?:additional generation preference:|keep character identity|preserve character identity|the user's explicit clothing)|\.\s*Negative prompt:|$)",
        flags=re.I | re.S,
    )

    def clean_visual_memory(match: re.Match[str]) -> str:
        cleaned = _scene_without_daily_outfit_details(match.group(2))
        return f"{match.group(1)}{cleaned}" if cleaned else ""

    text = visual_memory_pattern.sub(clean_visual_memory, text)
    return re.sub(r",\s*,+", ",", text)


def _outfit_label(category: str) -> str:
    return _CATEGORY_LABELS.get(_clean_text(category, 80).lower(), "the requested outfit")


def _explicit_mirror_request(text: str) -> bool:
    raw = _clean_text(text, 1200)
    if not raw:
        return False
    detection_text = re.split(r"negative prompt\s*:", raw.lower(), maxsplit=1, flags=re.I)[0]
    positive_scan = re.sub(
        r"(?:不要|避免|别|不许|禁止).{0,18}(?:镜前|对镜|镜中|镜子|全身镜|穿衣镜|试衣镜)",
        " ",
        detection_text,
        flags=re.I,
    )
    positive_scan = re.sub(
        r"(?:no|not|avoid|without)\s+(?:a\s+)?(?:mirror|mirror\s+selfie|full[-\s]?length\s+mirror|"
        r"full[-\s]?body\s+mirror|mirror\s+shot|mirror\s+photo|mirror\s+portrait)[^,.;；。]*",
        " ",
        positive_scan,
        flags=re.I,
    )
    positive_scan = re.sub(r"\bnon[-\s]?mirror\b", " ", positive_scan, flags=re.I)
    positive_scan = re.sub(r"unless[^,.;；。]*mirror[^,.;；。]*", " ", positive_scan, flags=re.I)
    return bool(
        re.search(
            r"镜前|对镜|镜中|镜子|全身镜|穿衣镜|试衣镜|\bmirror\b|looking\s+in\s+the\s+mirror|in\s+front\s+of\s+(?:a\s+)?mirror",
            positive_scan,
            flags=re.I,
        )
    )


def _automatic_presets(
    workflow_kind: str,
    intent: PhotoWardrobeIntent,
    excluded_categories: Collection[str],
) -> tuple[str, ...]:
    kind = _clean_text(workflow_kind, 40).lower()
    if kind in _EDIT_WORKFLOWS:
        return ()
    excluded = set(excluded_categories) | set(intent.excluded_categories)
    target_preset = _CATEGORY_PRESETS.get(intent.target_category, "")
    if target_preset and intent.target_category not in excluded:
        return (target_preset,)
    text = intent.positive_text.lower()
    if kind in _SELFIE_WORKFLOWS:
        if any(token in text for token in ("表情包", "贴纸", "sticker", "meme")):
            return ("表情包场景",)
        if re.search(r"(?<![a-z0-9])cos(?:play)?(?![a-z0-9])|角色扮演|扮成|神灯|女仆|巫女|魔法少女", text, flags=re.I):
            return ("COS自拍",)
        if _explicit_mirror_request(text):
            return ("镜前穿搭",)
        if any(token in text for token in ("穿搭", "衣服", "外套", "校服", "裙", "outfit", "clothes", "jacket", "uniform", "skirt")):
            return ("日常穿搭",)
        if any(token in text for token in ("头像", "特写", "大头", "avatar", "close-up", "closeup", "profile picture")):
            return ("头像特写",)
        return ("角色自拍",)
    if any(token in text for token in ("表情包", "贴纸", "sticker", "meme")):
        return ("表情包场景",)
    if any(token in text for token in ("房间", "桌", "书", "杯", "床", "窗边", "室内", "room", "desk", "book", "cup", "bed", "window", "indoor")):
        return ("房间日常",)
    return ("可拍画面",)


def _selected_presets(
    *,
    workflow_kind: str,
    intent: PhotoWardrobeIntent,
    preset_name: str,
    available_presets: Collection[str],
    excluded_categories: Collection[str],
) -> tuple[str, ...]:
    available = {_clean_text(name, 80) for name in available_presets if _clean_text(name, 80)}
    if intent.requested_scene_preset and intent.requested_scene_preset in available:
        return (intent.requested_scene_preset,)
    if preset_name and preset_name in available:
        return (preset_name,)
    return tuple(
        name
        for name in _automatic_presets(workflow_kind, intent, excluded_categories)
        if name in available
    )[:3]


def _clean_decision_context(
    *,
    base_prompt: str,
    prompt_text: str,
    scene_context: str,
    remove_daily_outfit: bool,
) -> tuple[str, str, tuple[str, ...]]:
    cleaned_prompt = str(base_prompt or prompt_text or "").strip()
    cleaned_scene = _clean_text(scene_context, 2400)
    adjustments: list[str] = []
    if remove_daily_outfit and _DAILY_OUTFIT_PATTERN.search(cleaned_scene):
        updated_scene = _scene_without_daily_outfit_details(cleaned_scene)
        if updated_scene != cleaned_scene:
            cleaned_scene = updated_scene
            adjustments.append("daily_outfit_context_removed")
    if remove_daily_outfit:
        updated_prompt = _prompt_without_generated_daily_outfit_continuity(cleaned_prompt)
        if updated_prompt != cleaned_prompt:
            cleaned_prompt = updated_prompt
            adjustments.append("generated_daily_outfit_continuity_removed")
    return cleaned_prompt, cleaned_scene, tuple(adjustments)


def _daily_outfit_context_is_applicable(prompt_text: str, scene_context: str) -> bool:
    prompt = _clean_text(prompt_text, 1800)
    text = _clean_text(f"{prompt}；{scene_context}", 3600)
    if re.search(
        r"(?:展示|看看|晒|拍).{0,12}(?:今日|今天|当天).{0,8}(?:穿搭|衣服|服装|造型)"
        r"|(?:今日|今天|当天).{0,8}(?:穿搭|衣服|服装|造型).{0,12}(?:展示|看看|晒|拍)"
        r"|\b(?:show(?:ing)?\s+off\s+today'?s\s+outfit|show\s+(?:me\s+)?today'?s\s+outfit|outfit\s+check|ootd)\b",
        prompt,
        flags=re.I,
    ):
        return True
    if re.search(
        r"卧室|在家|家里|居家|睡前|临睡|准备睡|刚起床|刚醒|起床后"
        r"|\b(?:bedroom|at\s+home|bedtime|before\s+bed|just\s+woke|waking\s+up)\b",
        text,
        flags=re.I,
    ):
        return False
    return bool(
        re.search(
            r"外出|出门|通勤|上学|上班|逛街|购物|商场|街头|街边|旅行|旅游|公园|户外|散步"
            r"|\b(?:outdoors?|going\s+out|commut(?:e|ing)|school|class|work|office|shopping|mall|street|"
            r"travel|trip|park|walk(?:ing)?)\b",
            text,
            flags=re.I,
        )
    )


def resolve_photo_wardrobe_decision(
    *,
    workflow_kind: str,
    prompt_text: str,
    reference: Mapping[str, Any] | None,
    scene_context: str = "",
    requested_scene_preset: str = "",
    intent: PhotoWardrobeIntent | None = None,
    base_prompt: str = "",
    available_presets: Collection[str] = (),
) -> PhotoWardrobeDecision:
    resolved_intent = intent or analyze_photo_wardrobe(prompt_text, requested_scene_preset)
    if requested_scene_preset and resolved_intent.requested_scene_preset != _clean_text(requested_scene_preset, 80):
        raise ValueError("intent does not match requested_scene_preset")

    normalized_kind = _clean_text(workflow_kind, 40).lower()
    reference_data = dict(reference or {})
    reference_path = _clean_text(reference_data.get("path"), 1000)
    reference_id = _clean_text(reference_data.get("id"), 60)
    reference_kind = _clean_text(reference_data.get("kind"), 40)
    roles = tuple(str(role) for role in (reference_data.get("reference_roles") or ()))
    effective_roles = roles
    adjustments: list[str] = []
    reference_category = _clean_text(reference_data.get("outfit_category"), 40).lower()
    reference_locks = bool(reference_data.get("outfit_lock_default")) and "outfit" in roles
    preset_category = resolved_intent.requested_preset_category
    preset_name = resolved_intent.requested_scene_preset if preset_category else ""
    remove_daily = bool(preset_category and preset_category != "daily_outfit")

    if normalized_kind not in _SELFIE_WORKFLOWS:
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name="",
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        return PhotoWardrobeDecision(
            rule_id="non_selfie_source_edit" if normalized_kind in _EDIT_WORKFLOWS and reference_path else "non_selfie",
            mode="source_edit" if normalized_kind in _EDIT_WORKFLOWS and reference_path else "none",
            source="explicit_reference" if reference_path else "none",
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=roles,
            reason="non-selfie workflow keeps its own image-edit contract",
            base_prompt=str(base_prompt or prompt_text or "").strip(),
            scene_context=_clean_text(scene_context, 2400),
        )

    if preset_category:
        if reference_category != preset_category and "outfit" in effective_roles:
            effective_roles = tuple(role for role in effective_roles if role != "outfit")
            adjustments.append("reference_outfit_role_removed")
        effective_exclusions = tuple(
            category for category in resolved_intent.excluded_categories if category != preset_category
        )
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=remove_daily,
        )
        adjustments.extend(context_adjustments)
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name=preset_name,
            available_presets=available_presets,
            excluded_categories=effective_exclusions,
        )
        category_label = _outfit_label(preset_category)
        return PhotoWardrobeDecision(
            rule_id="explicit_scene_preset",
            mode="explicit_preset",
            source="requested_scene_preset",
            category=preset_category,
            lock_outfit=True,
            remove_daily_outfit_context=remove_daily,
            preset_name=preset_name,
            authoritative_preset=preset_name,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=effective_roles,
            positive_instruction=(
                f"The explicitly requested scene preset '{preset_name}' is an authoritative wardrobe request. "
                f"Render exactly one coherent {category_label} outfit; use a matching outfit reference when available, "
                "and use an incompatible reference only for identity and other compatible details."
            ),
            negative_instruction=(
                "Do not restore clothing from today's outfit, schedule context, an older photo, or an incompatible reference. "
                "Do not reinterpret the requested preset as a negative prompt or an excluded wardrobe category."
                if remove_daily
                else "Do not replace today's requested outfit with an unrelated costume or wardrobe."
            ),
            reason="structured scene preset explicitly controls the wardrobe",
            excluded_categories=effective_exclusions,
            requested_outfit_text=preset_name,
            base_prompt=cleaned_prompt,
            scene_context=cleaned_scene,
            adjustments=tuple(adjustments),
        )

    explicit_category = resolved_intent.target_category
    if explicit_category:
        if (
            explicit_category == "custom_outfit"
            or reference_category != explicit_category
        ) and "outfit" in effective_roles:
            effective_roles = tuple(role for role in effective_roles if role != "outfit")
            adjustments.append("reference_outfit_role_removed")
        remove_daily = explicit_category != "daily_outfit"
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=remove_daily,
        )
        adjustments.extend(context_adjustments)
        preset_name = _CATEGORY_PRESETS.get(explicit_category, "")
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name=preset_name,
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        exclusion_instruction = (
            f"Respect the current request's explicit wardrobe exclusions: {resolved_intent.exclusion_text}."
            if resolved_intent.exclusion_text
            else ""
        )
        return PhotoWardrobeDecision(
            rule_id="explicit_prompt",
            mode="explicit_prompt",
            source="user_prompt",
            category=explicit_category,
            lock_outfit=True,
            remove_daily_outfit_context=remove_daily,
            preset_name=preset_name,
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=effective_roles,
            positive_instruction=(
                "An explicit clothing request in this prompt has highest priority. "
                f"Render one coherent {_outfit_label(explicit_category)} outfit exactly as requested; "
                "use any incompatible selected reference only for identity and compatible visual details."
            ),
            negative_instruction=" ".join(
                part
                for part in (
                    (
                        "Do not restore clothing from today's outfit, schedule context, an older photo, or an incompatible reference."
                        if remove_daily
                        else "Do not replace today's requested outfit with an unrelated costume or wardrobe."
                    ),
                    exclusion_instruction,
                )
                if part
            ),
            reason=(
                "explicit custom or generic clothing change in the current image prompt"
                if explicit_category == "custom_outfit"
                else "explicit clothing request in the current image prompt"
            ),
            excluded_categories=resolved_intent.excluded_categories,
            requested_outfit_text=resolved_intent.target_text,
            base_prompt=cleaned_prompt,
            scene_context=cleaned_scene,
            adjustments=tuple(adjustments),
        )

    excluded_categories = set(resolved_intent.excluded_categories)
    reference_outfit_excluded = bool(reference_category and reference_category in excluded_categories)
    scene_outfit_categories = {
        category for category, *_ in _outfit_category_matches(scene_context)
    }
    scene_daily_outfit_excluded = bool(
        _DAILY_OUTFIT_PATTERN.search(str(scene_context or ""))
        and (
            scene_outfit_categories.intersection(excluded_categories)
            or _daily_outfit_matches_custom_exclusion(
                scene_context,
                resolved_intent.exclusion_text,
            )
        )
    )
    compatible_locked_reference = bool(
        reference_locks
        and reference_category
        and reference_category not in excluded_categories
        and reference_kind != "daily_outfit"
    )
    unknown_locked_reference = bool(reference_locks and not reference_category and excluded_categories)

    if scene_daily_outfit_excluded and compatible_locked_reference:
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=True,
        )
        base_prompt = cleaned_prompt
        scene_context = cleaned_scene
        adjustments.extend(context_adjustments)

    if (
        reference_outfit_excluded
        or unknown_locked_reference
        or (scene_daily_outfit_excluded and not compatible_locked_reference)
    ):
        if "outfit" in effective_roles and (
            reference_outfit_excluded or unknown_locked_reference or reference_kind == "daily_outfit"
        ):
            effective_roles = tuple(role for role in effective_roles if role != "outfit")
            adjustments.append("reference_outfit_role_removed")
        remove_daily = reference_category == "daily_outfit" or scene_daily_outfit_excluded
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=remove_daily,
        )
        adjustments.extend(context_adjustments)
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name="",
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        exclusion_instruction = (
            f"Respect the current request's explicit wardrobe exclusions: {resolved_intent.exclusion_text}."
            if resolved_intent.exclusion_text
            else ""
        )
        return PhotoWardrobeDecision(
            rule_id="explicit_exclusion",
            mode="explicit_exclusion",
            source="user_prompt",
            remove_daily_outfit_context=remove_daily,
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=effective_roles,
            positive_instruction=(
                "Use the selected reference and schedule context only for responsibilities compatible with the current request; "
                "do not use wardrobe details that the current request explicitly excludes."
            ),
            negative_instruction=exclusion_instruction,
            reason=(
                "selected reference outfit is explicitly excluded by the current request"
                if reference_outfit_excluded or unknown_locked_reference
                else "daily outfit context conflicts with an explicit wardrobe exclusion"
            ),
            excluded_categories=resolved_intent.excluded_categories,
            base_prompt=cleaned_prompt,
            scene_context=cleaned_scene,
            adjustments=tuple(adjustments),
        )

    if reference_kind == "daily_outfit":
        preset_name = "日常穿搭"
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name=preset_name,
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        exclusion_instruction = (
            f"Respect the current request's explicit wardrobe exclusions: {resolved_intent.exclusion_text}."
            if resolved_intent.exclusion_text
            else ""
        )
        return PhotoWardrobeDecision(
            rule_id="daily_outfit_reference",
            mode="daily_outfit",
            source="selected_reference",
            category="daily_outfit",
            lock_outfit=True,
            preset_name=preset_name,
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=roles,
            positive_instruction=(
                "Use the selected reference as the authoritative source for today's complete outfit and identity continuity. "
                "Preserve its coherent clothing layers, accessories, silhouette, and main color palette."
            ),
            negative_instruction=" ".join(
                part
                for part in (
                    "Do not invent an alternative outfit or mix several wardrobe variants.",
                    exclusion_instruction,
                )
                if part
            ),
            reason="selected reference is today's outfit reference",
            excluded_categories=resolved_intent.excluded_categories,
            base_prompt=str(base_prompt or prompt_text or "").strip(),
            scene_context=_clean_text(scene_context, 2400),
        )

    if reference_kind == "recent_sent_photo" and reference_locks:
        category = reference_category or "reference_outfit"
        remove_daily = category != "daily_outfit"
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=remove_daily,
        )
        adjustments.extend(context_adjustments)
        preset_name = _clean_text(reference_data.get("preferred_preset"), 60) or _CATEGORY_PRESETS.get(
            category,
            "",
        )
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name=preset_name,
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        exclusion_instruction = (
            f"Respect the current request's explicit wardrobe exclusions: {resolved_intent.exclusion_text}."
            if resolved_intent.exclusion_text
            else ""
        )
        return PhotoWardrobeDecision(
            rule_id="recent_photo_continuity",
            mode="continuity",
            source="selected_reference",
            category=category,
            lock_outfit=True,
            remove_daily_outfit_context=remove_daily,
            preset_name=preset_name,
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=roles,
            positive_instruction=(
                "This is the last image actually sent in the same conversation. Use it as the authoritative continuity source "
                "for identity, the complete outfit, room or location, lighting, and time of day unless the current request explicitly changes one of them. "
                "Use the current schedule only to fill details that are missing and non-conflicting."
            ),
            negative_instruction=" ".join(
                part
                for part in (
                    "Do not relocate the scene, redesign the outfit, or replace continuity details merely because the schedule has advanced.",
                    exclusion_instruction,
                )
                if part
            ),
            reason="selected reference is the last image sent in this conversation",
            excluded_categories=resolved_intent.excluded_categories,
            base_prompt=cleaned_prompt,
            scene_context=cleaned_scene,
            adjustments=tuple(adjustments),
        )

    if reference_locks:
        category = reference_category or "reference_outfit"
        remove_daily = category != "daily_outfit"
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=remove_daily,
        )
        adjustments.extend(context_adjustments)
        preset_name = _clean_text(reference_data.get("preferred_preset"), 60) or _CATEGORY_PRESETS.get(
            category,
            "",
        )
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name=preset_name,
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        exclusion_instruction = (
            f"Respect the current request's explicit wardrobe exclusions: {resolved_intent.exclusion_text}."
            if resolved_intent.exclusion_text
            else ""
        )
        return PhotoWardrobeDecision(
            rule_id="locked_reference_outfit",
            mode="reference_outfit",
            source="selected_reference",
            category=category,
            lock_outfit=True,
            remove_daily_outfit_context=remove_daily,
            preset_name=preset_name,
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=roles,
            positive_instruction=(
                "Use the selected reference image as the authoritative source for identity and the complete visible outfit. "
                f"Preserve {_outfit_label(category)}, including its garment category, layers, silhouette, material impression, "
                "trim details, accessories, and main color palette. The schedule context controls only location, activity, mood, lighting, and time."
            ),
            negative_instruction=" ".join(
                part
                for part in (
                    (
                        "Do not replace the selected-reference outfit with today's daytime outfit, school or commuter layers, a coat, blazer, shirt, vest, tie, or another wardrobe unless the user explicitly requests it."
                        if category in {"sleepwear", "homewear"}
                        else "Do not restore a different outfit from schedule context or today's outfit."
                    ),
                    exclusion_instruction,
                )
                if part
            ),
            reason="selected reference is an outfit-bearing reference with outfit_lock_default=true",
            excluded_categories=resolved_intent.excluded_categories,
            base_prompt=cleaned_prompt,
            scene_context=cleaned_scene,
            adjustments=tuple(adjustments),
        )

    daily_outfit_context_removed = False
    daily_outfit_context_available = bool(
        _DAILY_OUTFIT_PATTERN.search(str(scene_context or ""))
    )
    if daily_outfit_context_available and not _daily_outfit_context_is_applicable(
        resolved_intent.positive_text, scene_context
    ):
        adjustments.append("daily_outfit_context_not_applicable")
        cleaned_prompt, cleaned_scene, context_adjustments = _clean_decision_context(
            base_prompt=base_prompt,
            prompt_text=prompt_text,
            scene_context=scene_context,
            remove_daily_outfit=True,
        )
        base_prompt = cleaned_prompt
        scene_context = cleaned_scene
        adjustments.extend(context_adjustments)
        daily_outfit_context_removed = True

    if _DAILY_OUTFIT_PATTERN.search(str(scene_context or "")):
        preset_name = "日常穿搭"
        selected = _selected_presets(
            workflow_kind=workflow_kind,
            intent=resolved_intent,
            preset_name=preset_name,
            available_presets=available_presets,
            excluded_categories=resolved_intent.excluded_categories,
        )
        return PhotoWardrobeDecision(
            rule_id="daily_outfit_context",
            mode="daily_outfit_context",
            source="daily_outfit",
            category="daily_outfit",
            lock_outfit=False,
            preset_name=preset_name,
            authoritative_preset=resolved_intent.requested_scene_preset,
            selected_presets=selected,
            reference_image_path=reference_path,
            reference_id=reference_id,
            reference_kind=reference_kind,
            reference_roles=roles,
            effective_reference_roles=roles,
            positive_instruction=(
                "The selected reference, if present, controls identity only. Since the user did not request a clothing change, "
                "today's outfit context may provide wardrobe continuity."
            ),
            negative_instruction="Do not copy incidental clothing from an identity-only reference over today's outfit.",
            reason="identity-only reference with available daily outfit context",
            excluded_categories=resolved_intent.excluded_categories,
            base_prompt=str(base_prompt or prompt_text or "").strip(),
            scene_context=_clean_text(scene_context, 2400),
        )

    selected = _selected_presets(
        workflow_kind=workflow_kind,
        intent=resolved_intent,
        preset_name="",
        available_presets=available_presets,
        excluded_categories=resolved_intent.excluded_categories,
    )
    return PhotoWardrobeDecision(
        rule_id="identity_only" if reference_path else "no_wardrobe_source",
        mode="identity_only" if reference_path else "none",
        source="selected_reference" if reference_path else "none",
        remove_daily_outfit_context=daily_outfit_context_removed,
        authoritative_preset=resolved_intent.requested_scene_preset,
        selected_presets=selected,
        reference_image_path=reference_path,
        reference_id=reference_id,
        reference_kind=reference_kind,
        reference_roles=roles,
        effective_reference_roles=roles,
        positive_instruction=(
            "Use the selected reference only for character identity and appearance traits; its incidental clothing is not an outfit lock."
            if reference_path
            else ""
        ),
        reason="selected reference is identity-only" if reference_path else "no wardrobe source selected",
        excluded_categories=resolved_intent.excluded_categories,
        base_prompt=str(base_prompt or prompt_text or "").strip(),
        scene_context=_clean_text(scene_context, 2400),
        adjustments=tuple(adjustments),
    )

"""Deterministic roleplay-strength mapping for the Companion persona.

The persona should exaggerate (play along) when a group is teasing or
bantering, and dampen when the group is serious, tense, silent, or when a
member has asked to stop.  This module maps the group mood and the current
expression band into a single ``exaggerate`` scalar plus a human-readable
voice instruction for prompt injection.  It owns no persistence, clock,
network, or platform access.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .group_mood import MOOD_LABELS, project_group_mood


ROLEPLAY_STRENGTH_VERSION = "roleplay_strength.v1"

# Base exaggeration per mood (0-100).  The persona leans into playful moods,
# and steps back from serious / tense / silent ones.
_MOOD_BASE_EXAGGERATE = {
    "tease": 72.0,
    "banter": 66.0,
    "confession": 38.0,
    "serious": 14.0,
    "tension": 10.0,
    "dead_silence": 8.0,
}

# Expression-band modifiers: warmer bands allow more playfulness, reserved
# bands cap it.
_BAND_MODIFIER = {
    "avoidant": -40.0,
    "hurt": -50.0,
    "relaxed": 0.0,
    "lively": 16.0,
    "warm": 8.0,
    "close": 10.0,
    "affectionate": 4.0,
}

# Social-tension penalty: the hotter the air, the less we add fire.
_TENSION_DAMPEN_FACTOR = 0.5


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _band(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _BAND_MODIFIER else "relaxed"


def project_roleplay_strength(
    mood: Any,
    *,
    expression_band: Any = "relaxed",
    now: Any = None,
) -> dict[str, Any]:
    """Project a mood snapshot into a roleplay/exaggeration instruction.

    Args:
        mood: A group-mood snapshot (or raw mapping) as produced by
            :func:`group_mood.settle_group_mood`.
        expression_band: Current expression band value.
        now: Optional timestamp used for mood projection.
    """
    projected = project_group_mood(mood, now=now)
    if not projected:
        projected = {"mood_scores": {}, "top_mood": "dead_silence", "social_tension": 0.0}
    top_mood = str(projected.get("top_mood") or "dead_silence")
    if top_mood not in MOOD_LABELS:
        top_mood = "dead_silence"
    band = _band(expression_band)
    scores = projected.get("mood_scores") or {}
    # Blend the base mood exaggeration with the dominant mood weight so a weak
    # tease signal does not crank the persona to full meme mode.
    dominant = _finite(scores.get(top_mood), 0.0)
    base = _MOOD_BASE_EXAGGERATE.get(top_mood, 8.0)
    weight = min(1.0, max(0.0, dominant) / 50.0) if top_mood != "dead_silence" else min(1.0, dominant / 60.0)
    blended = base * weight + 8.0 * (1.0 - weight)
    modifier = _BAND_MODIFIER.get(band, 0.0)
    exaggerate = max(0.0, min(100.0, blended + modifier))

    # Tension dampening: keep the persona from escalating confrontation.
    tension = _finite(projected.get("social_tension"))
    if tension > 0:
        exaggerate = max(0.0, exaggerate - tension * _TENSION_DAMPEN_FACTOR * 0.3)

    band_text = {
        "avoidant": "收敛回避",
        "hurt": "低落收敛",
        "relaxed": "自然松弛",
        "lively": "活泼放开",
        "warm": "温和放开",
        "close": "亲近放开",
        "affectionate": "亲昵克制",
    }.get(band, "自然")

    if exaggerate >= 60:
        voice = "群聊正处在玩闹气氛，可以适度夸张、接梗、起哄，让回复更有参与感；不要刻意抢话或攻击谁"
    elif exaggerate >= 35:
        voice = "群聊气氛轻松，可以带一点俏皮和接梗，但保持自然，不强行玩梗"
    elif exaggerate >= 12:
        voice = "群聊气氛偏正或偏冷，保持克制、回应分寸，不开玩笑"
    else:
        voice = "群聊气氛紧张或沉默，只做必要回应，压低表达强度，不制造冲突"

    return {
        "version": ROLEPLAY_STRENGTH_VERSION,
        "exaggerate": round(exaggerate, 2),
        "top_mood": top_mood,
        "expression_band": band,
        "social_tension": round(tension, 2),
        "voice": voice,
        "band_voice": band_text,
    }


__all__ = ["ROLEPLAY_STRENGTH_VERSION", "project_roleplay_strength"]
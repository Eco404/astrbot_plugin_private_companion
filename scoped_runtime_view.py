"""Pure helpers for composing reconciled REQ-041 runtime views.

The helpers deliberately have no AstrBot dependency so the namespace boundary
can be tested independently from the host runtime.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from expression_scope_ownership import runtime_binding_is_approved


PRIVATE_FIELDS = frozenset({
    "nickname", "style", "profile_origin", "auto_profile_created",
    "companion_memory", "intent_profile", "dialogue_episodes", "open_loops",
    "behavior_habits", "action_preferences", "action_consequences", "state_continuity",
    "recent_reply_topics", "expression_profile",
    "profile_fact_revision",
})

GROUP_SHARED_FIELDS = frozenset({
    "recent_messages", "slang_terms", "slang_meanings", "topic_signatures", "topic_threads",
    "group_episodes", "relationship_edges", "atmosphere", "interjection_feedback",
    "expression_profile",
})

GROUP_MEMBER_FIELDS = frozenset({
    "name", "identity_name", "group_role", "group_role_label", "count", "last_seen",
    "display_name_events", "recent_phrases",
})


def _projection_fields(projection: Any) -> dict[str, Any] | None:
    if not isinstance(projection, dict) or projection.get("ok") is not True:
        return None
    fields = projection.get("fields")
    return fields if isinstance(fields, dict) else {}


def overlay_private_runtime_view(base: Any, projection: Any) -> Any:
    """Overlay only allowlisted private-domain fields from a reconciled projection."""
    if not isinstance(base, dict):
        return base
    fields = _projection_fields(projection)
    if fields is None:
        return base
    view = dict(base)
    for key, value in fields.items():
        if key in PRIVATE_FIELDS:
            view[key] = deepcopy(value)
    view["req041_scoped_read_generation"] = "new"
    return view


def overlay_group_runtime_view(
    base: Any,
    shared_projection: Any,
    *,
    sender_id: str = "",
    member_projection: Any = None,
) -> Any:
    """Compose one group view without admitting private or another-group fields."""
    if not isinstance(base, dict):
        return base
    shared_fields = _projection_fields(shared_projection)
    member_fields = _projection_fields(member_projection)
    if shared_fields is None and member_fields is None:
        return base
    view = deepcopy(base)
    if shared_fields is not None:
        for key, value in shared_fields.items():
            if key in GROUP_SHARED_FIELDS:
                view[key] = deepcopy(value)
    if member_fields is not None and sender_id:
        members = view.setdefault("members", {})
        if isinstance(members, dict):
            member = dict(members.get(sender_id) or {})
            for key, value in member_fields.items():
                if key in GROUP_MEMBER_FIELDS:
                    member[key] = deepcopy(value)
            members[sender_id] = member
    view["req041_scoped_read_generation"] = "new"
    return view


def scoped_approved_expression_rules(context_owner: Any) -> list[dict[str, Any]] | None:
    """Return current-namespace rules, or ``None`` when legacy selection still owns the read.

    An empty list is authoritative for a reconciled namespace and therefore
    must not fall back to the legacy cross-source aggregate.  The same
    fail-closed empty result applies while an already-bound scoped namespace
    is being reconciled after a write.
    """
    if not isinstance(context_owner, dict):
        return None
    generation = context_owner.get("req041_scoped_read_generation")
    if generation not in {"new", "new_unavailable"}:
        return None
    if generation == "new_unavailable":
        return []
    profile = context_owner.get("expression_profile")
    if not isinstance(profile, dict):
        return []
    rules = profile.get("learned_rules")
    if not isinstance(rules, list):
        return []
    return [
        deepcopy(item) for item in rules
        if isinstance(item, dict) and runtime_binding_is_approved(item.get("scope_binding"))
    ]


__all__ = [
    "GROUP_MEMBER_FIELDS", "GROUP_SHARED_FIELDS", "PRIVATE_FIELDS",
    "overlay_group_runtime_view", "overlay_private_runtime_view",
    "scoped_approved_expression_rules",
]

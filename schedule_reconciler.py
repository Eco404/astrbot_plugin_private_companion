# -*- coding: utf-8 -*-
"""Deterministic plan/actual reconciliation for the local C3 agenda."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

try:
    from .agenda_contracts import (
        agenda_entry_from_activity,
        agenda_entry_from_plan,
        interval_overlaps_window,
        normalize_observed_activity,
        normalize_plan_item,
        parse_datetime,
        stable_id,
    )
except ImportError:
    from agenda_contracts import (
        agenda_entry_from_activity,
        agenda_entry_from_plan,
        interval_overlaps_window,
        normalize_observed_activity,
        normalize_plan_item,
        parse_datetime,
        stable_id,
    )


def _normalized_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _tokens(value: Any) -> set[str]:
    text = _normalized_text(value)
    if not text:
        return set()
    tokens = {text}
    tokens.update(text[index:index + 2] for index in range(max(0, len(text) - 1)))
    tokens.update(text[index:index + 3] for index in range(max(0, len(text) - 2)))
    return {token for token in tokens if len(token) >= 2}


def _interval(item: dict[str, Any], now: datetime) -> tuple[datetime | None, datetime | None]:
    try:
        start = parse_datetime(item.get("start_at") or item.get("start"), default=now)
    except Exception:
        return None, None
    try:
        end = parse_datetime(item.get("end_at") or item.get("end"), default=start)
    except Exception:
        end = start
    if end <= start:
        end = start + timedelta(seconds=1)
    return start, end


def _overlaps(plan: dict[str, Any], activity: dict[str, Any], now: datetime) -> bool:
    ps, pe = _interval(plan, now)
    a_s, a_e = _interval(activity, now)
    return bool(ps and pe and a_s and a_e and ps < a_e and a_s < pe)


def _title_similarity(plan: dict[str, Any], activity: dict[str, Any]) -> float:
    plan_text = _normalized_text(plan.get("title") or plan.get("activity"))
    activity_text = _normalized_text(activity.get("title") or activity.get("summary"))
    if not plan_text or not activity_text:
        return 0.0
    if plan_text in activity_text or activity_text in plan_text:
        return 1.0
    shared = _tokens(plan_text).intersection(_tokens(activity_text))
    if not shared:
        return 0.0
    return len(shared) / max(1, len(_tokens(plan_text).union(_tokens(activity_text))))


def _same_source(plan: dict[str, Any], activity: dict[str, Any]) -> bool:
    plan_id = str(plan.get("plan_id") or plan.get("event_id") or "").strip()
    plan_refs = {str(ref) for ref in (plan.get("source_refs") or []) if str(ref)}
    activity_refs = {str(ref) for ref in (activity.get("source_refs") or []) if str(ref)}
    return bool((plan_id and plan_id in activity_refs) or plan_refs.intersection(activity_refs))


def _plan_is_past(plan: dict[str, Any], now: datetime) -> bool:
    _start, end = _interval(plan, now)
    return bool(end and end <= now)


def reconcile(
    plans: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    normalized_plans: list[dict[str, Any]] = []
    for raw in plans or []:
        try:
            normalized_plans.append(normalize_plan_item(raw, now=now))
        except Exception:
            continue
    normalized_activities: list[dict[str, Any]] = []
    for raw in activities or []:
        try:
            normalized_activities.append(normalize_observed_activity(raw, now=now))
        except Exception:
            continue

    used_activity_ids: set[str] = set()
    reconciled_plans: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    matched: dict[str, list[str]] = {}

    for plan in normalized_plans:
        plan_id = str(plan.get("plan_id") or stable_id("plan", plan.get("title")))
        exact = [activity for activity in normalized_activities if _same_source(plan, activity)]
        overlap_candidates = [activity for activity in normalized_activities if _overlaps(plan, activity, now)]
        similar = [activity for activity in overlap_candidates if _title_similarity(plan, activity) >= 0.18]
        selected = exact or similar
        plan_result = dict(plan)
        if selected:
            ids = [str(item.get("activity_id")) for item in selected]
            matched[plan_id] = ids
            used_activity_ids.update(ids)
            all_completed = all(str(item.get("status")) == "completed" for item in selected)
            plan_result["status"] = "completed" if all_completed else "active"
            reason = "plan_id/source_refs matched" if exact else "time overlap and controlled title similarity"
            plan_result["reconciliation_reason"] = reason
            plan_result["reconciled_activity_ids"] = ids
            reconciliation = {
                "reconciliation_id": stable_id("reconciliation", plan_id, ids),
                "plan_id": plan_id,
                "status": plan_result["status"],
                "source_kind": "reconciled",
                "evidence_level": "L3" if any(item.get("evidence_level") in {"L3", "L4", "L5"} for item in selected) else "L2",
                "source_refs": [plan_id, *ids],
                "activity_ids": ids,
                "reason": reason,
            }
            reconciliations.append(reconciliation)
            entries.append(agenda_entry_from_plan(plan_result, reason=reason))
            entries.extend(agenda_entry_from_activity(item, source_refs=[plan_id, *ids]) for item in selected)
        else:
            if plan_result.get("status") not in {"cancelled", "deferred"} and _plan_is_past(plan_result, now):
                plan_result["status"] = "unknown"
                plan_result["reconciliation_reason"] = "window ended without observed evidence"
            entries.append(agenda_entry_from_plan(plan_result, reason=plan_result.get("reconciliation_reason", "")))
        reconciled_plans.append(plan_result)

    for activity in normalized_activities:
        if str(activity.get("activity_id")) not in used_activity_ids:
            entries.append(agenda_entry_from_activity(activity))

    entries.sort(key=lambda item: (str(item.get("start_at") or ""), 0 if item.get("kind") == "observed" else 1))
    return {
        "plans": reconciled_plans,
        "activities": normalized_activities,
        "entries": entries,
        "matched": matched,
        "reconciliations": reconciliations,
    }

# -*- coding: utf-8 -*-
"""Read model and prompt formatter for the local C3 agenda."""
from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from .agenda_contracts import (
        SCHEDULE_WINDOWS,
        interval_overlaps_window,
        parse_datetime,
        window_bounds,
        window_for_datetime,
    )
    from .schedule_reconciler import reconcile
except ImportError:
    from agenda_contracts import SCHEDULE_WINDOWS, interval_overlaps_window, parse_datetime, window_bounds, window_for_datetime
    from schedule_reconciler import reconcile


def build_unified_agenda(
    *,
    plans: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    now: datetime,
    date_key: str = "",
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    result = reconcile(plans or [], activities or [], now=now)
    current_slug, current_window_date, _current_start, _current_end = window_for_datetime(now, timezone_name=timezone_name)
    target_date = date_key or current_window_date
    entries = [
        item for item in result["entries"]
        if not target_date or str(item.get("date") or item.get("start_at") or "")[:10] == target_date
    ]
    current = None
    for item in sorted(result["entries"], key=lambda value: str(value.get("start_at") or "")):
        if item.get("status") in {"cancelled", "unknown"}:
            continue
        try:
            start_text = item.get("start_at") or item.get("start")
            end_text = item.get("end_at") or item.get("end")
            item_start = parse_datetime(start_text, timezone_name=timezone_name)
            item_end = parse_datetime(end_text, timezone_name=timezone_name, default=item_start)
            if item_start <= now.astimezone(item_start.tzinfo) < item_end:
                current = item
                break
        except Exception:
            continue

    windows: list[dict[str, Any]] = []
    for slug, _name, _start_minute, _end_minute in SCHEDULE_WINDOWS:
        start, end = window_bounds(target_date, slug, timezone_name=timezone_name)
        window_plans = [item for item in result["plans"] if interval_overlaps_window(item, start, end, timezone_name=timezone_name)]
        window_activities = [item for item in result["activities"] if interval_overlaps_window(item, start, end, timezone_name=timezone_name)]
        window_reconciliations = [
            item for item in result["reconciliations"]
            if any(str(plan.get("plan_id")) in {str(p.get("plan_id")) for p in window_plans} for plan in [item])
        ]
        windows.append(
            {
                "slug": slug,
                "window": slug,
                "window_date": target_date,
                "start_at": start.isoformat(timespec="seconds"),
                "end_at": end.isoformat(timespec="seconds"),
                "planned": window_plans,
                "observed": window_activities,
                "reconciled": window_reconciliations,
            }
        )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "date": target_date,
        "window_date": target_date,
        "current_window": current_slug if target_date == current_window_date else "",
        "current": current,
        "entries": entries,
        "plans": result["plans"],
        "activities": result["activities"],
        "matched": result["matched"],
        "reconciliations": result["reconciliations"],
        "windows": windows,
    }


def format_agenda_context(agenda: dict[str, Any], *, max_entries: int = 8) -> str:
    if not isinstance(agenda, dict):
        return ""
    lines = [f"C3日程（{agenda.get('window_date') or agenda.get('date') or '未知日期'}）"]
    current = agenda.get("current")
    if isinstance(current, dict):
        lines.append(
            f"当前实际：{str(current.get('title') or '未命名')[:80]} "
            f"[{current.get('evidence_level') or 'L?'}|{current.get('status') or 'unknown'}]"
        )
    entries = agenda.get("entries") if isinstance(agenda.get("entries"), list) else []
    for item in entries[: max(0, int(max_entries))]:
        if not isinstance(item, dict):
            continue
        kind = "实际" if item.get("kind") == "observed" else "计划"
        title = str(item.get("title") or "未命名")[:80]
        status = str(item.get("status") or "unknown")
        reason = str(item.get("reconciliation_reason") or "")
        suffix = f"；{reason}" if reason else ""
        lines.append(f"- {kind}：{title} [{status}]{suffix}")
    if len(lines) == 1:
        lines.append("- 暂无记录")
    return "\n".join(lines)

# -*- coding: utf-8 -*-
"""Local C3 agenda contracts for the chat-side companion plugin.

The schedule definition is deliberately imported from ``bot_personal_contract``
so this module cannot drift into a second set of window thresholds.  Everything
stored by this module is a plain JSON-compatible dictionary or scalar.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import json
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:  # package import
    from .bot_personal_contract import (
        SCHEDULE_WINDOWS as _CONTRACT_WINDOWS,
        WINDOW_SLUGS as _CONTRACT_WINDOW_SLUGS,
        window_for_minutes as _contract_window_for_minutes,
    )
except ImportError:  # direct test/import from the plugin directory
    from bot_personal_contract import (
        SCHEDULE_WINDOWS as _CONTRACT_WINDOWS,
        WINDOW_SLUGS as _CONTRACT_WINDOW_SLUGS,
        window_for_minutes as _contract_window_for_minutes,
    )


AGENDA_VERSION = 1
SCHEDULE_WINDOWS = tuple(_CONTRACT_WINDOWS)
WINDOW_SLUGS = tuple(_CONTRACT_WINDOW_SLUGS)
SOURCE_KINDS = {"planned", "observed", "projection", "reconciled"}
EVIDENCE_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5"}
AGENDA_STATUSES = {
    "planned",
    "active",
    "completed",
    "partially_completed",
    "overridden",
    "reconciled",
    "deferred",
    "cancelled",
    "unknown",
}


class AgendaContractError(ValueError):
    """Raised when a value cannot satisfy the local agenda contract."""


def _text(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _list(value: Any, limit: int = 30) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item, 200)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result

def _items(value: Any, limit: int = 40) -> list[Any]:
    if not isinstance(value, list):
        return []
    return deepcopy(value[:limit])


def _version(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return current.isoformat(timespec="seconds")


def stable_id(prefix: str, *parts: Any) -> str:
    """Return a deterministic, order-stable identifier for JSON-like parts."""

    def canonical(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        if isinstance(value, set):
            return sorted(canonical(item) for item in value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return value

    raw = json.dumps(canonical(parts), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{_text(prefix, 48) or 'agenda'}-{digest}"


def timezone_or_default(timezone_name: Any = "Asia/Shanghai") -> ZoneInfo:
    try:
        return ZoneInfo(_text(timezone_name, 64) or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def normalize_window(value: Any) -> str:
    candidate = _text(value, 48).lower()
    return candidate if candidate in WINDOW_SLUGS else ""


def window_for_minutes(minutes: Any) -> str:
    """Delegate minute classification to the shared chat-side contract."""

    try:
        return _contract_window_for_minutes(int(minutes))
    except (TypeError, ValueError):
        return ""


def _window_spec(window: Any) -> tuple[str, str, int, int]:
    slug = normalize_window(window)
    for item in SCHEDULE_WINDOWS:
        if item[0] == slug:
            return item
    raise AgendaContractError(f"unknown window: {window!r}")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(_text(value, 32))
    except ValueError as exc:
        raise AgendaContractError(f"invalid date: {value!r}") from exc


def parse_datetime(value: Any, *, timezone_name: str = "Asia/Shanghai", default: datetime | None = None) -> datetime:
    """Parse ISO/date/time values and attach the requested local timezone."""

    tz = timezone_or_default(timezone_name)
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, date):
        current = datetime.combine(value, time.min)
    else:
        text = _text(value, 96)
        if not text:
            if default is None:
                raise AgendaContractError("datetime is required")
            current = default
        else:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                current = datetime.fromisoformat(text)
            except ValueError:
                try:
                    current = datetime.strptime(text, "%H:%M")
                except ValueError as exc:
                    raise AgendaContractError(f"invalid datetime: {value!r}") from exc
    if current.tzinfo is None:
        return current.replace(tzinfo=tz)
    return current.astimezone(tz)


def window_bounds(
    window_date: str | date,
    window: str,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    """Return inclusive-start/exclusive-end aware bounds for a window date."""

    _slug, _name, start_minute, end_minute = _window_spec(window)
    target = _as_date(window_date)
    tz = timezone_or_default(timezone_name)
    start = datetime.combine(target, time.min, tzinfo=tz) + timedelta(minutes=start_minute)
    end_date = target + timedelta(days=1) if end_minute <= start_minute else target
    end = datetime.combine(end_date, time.min, tzinfo=tz) + timedelta(minutes=end_minute)
    return start, end


def window_for_datetime(
    value: datetime | date | str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[str, str, datetime, datetime]:
    """Resolve a moment to ``(slug, window_date, start, end)``.

    The early-morning part of ``late_night`` belongs to the preceding
    ``window_date``.  Minute classification always goes through the shared
    ``bot_personal_contract.window_for_minutes`` implementation.
    """

    current = parse_datetime(value, timezone_name=timezone_name)
    minute = current.hour * 60 + current.minute
    slug = window_for_minutes(minute)
    if not slug:
        raise AgendaContractError(f"no window for minute: {minute}")
    _slug, _name, start_minute, end_minute = _window_spec(slug)
    belongs_to_previous_date = end_minute <= start_minute and minute < end_minute
    target_date = current.date() - timedelta(days=1) if belongs_to_previous_date else current.date()
    start, end = window_bounds(target_date, slug, timezone_name=timezone_name)
    return slug, target_date.isoformat(), start, end


def window_for_plan_minutes(
    plan_date: str | date,
    minutes: Any,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[str, str]:
    """Resolve a plan date plus possibly out-of-range minute offset."""

    try:
        base = _as_date(plan_date)
        value = int(minutes)
    except (TypeError, ValueError, AgendaContractError):
        return "", ""
    day_offset, raw_minute = divmod(value, 24 * 60)
    moment = datetime.combine(base + timedelta(days=day_offset), time.min)
    moment += timedelta(minutes=raw_minute)
    slug, target_date, _start, _end = window_for_datetime(moment, timezone_name=timezone_name)
    return slug, target_date


def interval_overlaps_window(
    item: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> bool:
    """Return whether an item interval intersects ``[start, end)``."""

    if not isinstance(item, dict):
        return False
    try:
        item_start = parse_datetime(item.get("start_at") or item.get("start"), timezone_name=timezone_name)
    except AgendaContractError:
        return False
    try:
        item_end = parse_datetime(
            item.get("end_at") or item.get("end"),
            timezone_name=timezone_name,
            default=item_start,
        )
    except AgendaContractError:
        item_end = item_start
    if item_end <= item_start:
        item_end = item_start + timedelta(seconds=1)
    start_local = parse_datetime(start, timezone_name=timezone_name)
    end_local = parse_datetime(end, timezone_name=timezone_name)
    return item_start < end_local and item_end > start_local


def normalize_source_kind(value: Any, default: str) -> str:
    candidate = _text(value, 32).lower()
    return candidate if candidate in SOURCE_KINDS else default


def normalize_evidence_level(value: Any, default: str) -> str:
    candidate = _text(value, 8).upper()
    return candidate if candidate in EVIDENCE_LEVELS else default


def _normalize_status(value: Any, default: str) -> str:
    candidate = _text(value, 32).lower()
    return candidate if candidate in AGENDA_STATUSES else default


def normalize_plan_item(
    raw: dict[str, Any],
    *,
    plan_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AgendaContractError("plan item must be an object")
    title = _text(raw.get("title") or raw.get("activity") or raw.get("description"), 240)
    if not title:
        raise AgendaContractError("plan item requires a title")
    result = deepcopy(raw)
    result.update(
        {
            "plan_id": _text(plan_id or raw.get("plan_id") or raw.get("event_id"), 120)
            or stable_id("plan", title, raw.get("date"), raw.get("time"), raw.get("start_at")),
            "title": title,
            "source_kind": "planned",
            "evidence_level": normalize_evidence_level(raw.get("evidence_level"), "L0"),
            "status": _normalize_status(raw.get("status"), "planned"),
            "version": _version(raw.get("version")),
            "source_refs": _list(raw.get("source_refs") or raw.get("basis")),
            "visibility": _text(raw.get("visibility"), 32) or "private",
            "certainty": _text(raw.get("certainty"), 24) or "medium",
            "updated_at": _text(raw.get("updated_at"), 64) or _now_iso(now),
        }
    )
    return result


def normalize_observed_activity(
    raw: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AgendaContractError("observed activity must be an object")
    title = _text(raw.get("title") or raw.get("summary") or raw.get("activity"), 240)
    if not title:
        raise AgendaContractError("observed activity requires a title")
    source_refs = _list(raw.get("source_refs") or raw.get("evidence_refs"))
    result = deepcopy(raw)
    result.update(
        {
            "activity_id": _text(raw.get("activity_id") or raw.get("id"), 120)
            or stable_id("activity", title, raw.get("start_at") or raw.get("start"), raw.get("end_at") or raw.get("end"), source_refs),
            "title": title,
            "kind": _text(raw.get("kind"), 48) or "conversation",
            "source_kind": "observed",
            "source": _text(raw.get("source"), 64) or "conversation",
            "source_refs": source_refs,
            "participants": _list(raw.get("participants")),
            "evidence_level": normalize_evidence_level(raw.get("evidence_level"), "L2"),
            "visibility": _text(raw.get("visibility"), 32) or "private",
            "certainty": _text(raw.get("certainty"), 24) or "medium",
            "status": _normalize_status(raw.get("status"), "active"),
            "version": _version(raw.get("version")),
            "updated_at": _text(raw.get("updated_at"), 64) or _now_iso(now),
        }
    )
    return result


def normalize_window_snapshot(
    raw: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AgendaContractError("window snapshot must be an object")
    window_date = _text(raw.get("window_date") or raw.get("date"), 20)
    window = normalize_window(raw.get("window") or raw.get("slug"))
    if not window_date or not window:
        raise AgendaContractError("window snapshot requires window_date and window")
    result = deepcopy(raw)
    result.update(
        {
            "snapshot_id": _text(raw.get("snapshot_id"), 160) or stable_id("agenda_snapshot", window_date, window),
            "date": _text(raw.get("date"), 20) or window_date,
            "window_date": window_date,
            "window": window,
            "planned": _items(raw.get("planned")),
            "observed": _items(raw.get("observed")),
            "reconciled": _items(raw.get("reconciled")),
            "open_items": _list(raw.get("open_items")),
            "source_refs": _list(raw.get("source_refs")),
            "source_kind": "projection",
            "evidence_level": normalize_evidence_level(raw.get("evidence_level"), "L2"),
            "visibility": _text(raw.get("visibility"), 32) or "private",
            "certainty": _text(raw.get("certainty"), 24) or "medium",
            "status": _normalize_status(raw.get("status"), "completed"),
            "version": _version(raw.get("version")),
            "generated_at": _text(raw.get("generated_at"), 64) or _now_iso(now),
            "timezone": _text(raw.get("timezone"), 64) or "Asia/Shanghai",
        }
    )
    return result


def normalize_reconciliation(raw: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AgendaContractError("reconciliation must be an object")
    window_date = _text(raw.get("window_date") or raw.get("date"), 20)
    window = normalize_window(raw.get("window") or raw.get("slug"))
    result = deepcopy(raw)
    result.update(
        {
            "reconciliation_id": _text(raw.get("reconciliation_id"), 160)
            or stable_id("reconciliation", window_date, window),
            "window_date": window_date,
            "date": _text(raw.get("date"), 20) or window_date,
            "window": window,
            "source_kind": "reconciled",
            "evidence_level": normalize_evidence_level(raw.get("evidence_level"), "L3"),
            "visibility": _text(raw.get("visibility"), 32) or "private",
            "certainty": _text(raw.get("certainty"), 24) or "high",
            "status": _normalize_status(raw.get("status"), "completed"),
            "source_refs": _list(raw.get("source_refs")),
            "version": _version(raw.get("version")),
            "generated_at": _text(raw.get("generated_at"), 64) or _now_iso(now),
        }
    )
    return result


def migrate_store(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Additive migration for old plugin JSON without deleting old fields."""

    if not isinstance(data, dict):
        raise AgendaContractError("store must be an object")
    changed = False
    defaults: dict[str, Any] = {
        "agenda_version": AGENDA_VERSION,
        "observed_activities": [],
        "window_snapshots": [],
        "agenda_reconciliation_history": [],
    }
    for key, default in defaults.items():
        if key not in data:
            data[key] = deepcopy(default)
            changed = True
        elif not isinstance(data.get(key), type(default)):
            data[f"legacy_{key}"] = deepcopy(data[key])
            data[key] = deepcopy(default)
            changed = True

    if "activities" in data and not data["observed_activities"] and isinstance(data["activities"], list):
        data["observed_activities"] = deepcopy(data["activities"])
        changed = True

    try:
        current_version = max(0, int(data.get("agenda_version") or 0))
    except (TypeError, ValueError):
        current_version = 0
    if current_version < AGENDA_VERSION:
        data["agenda_version"] = AGENDA_VERSION
        changed = True

    if isinstance(data.get("daily_plan"), dict) and isinstance(data["daily_plan"].get("items"), list):
        migrated_items: list[Any] = []
        for item in data["daily_plan"]["items"]:
            if not isinstance(item, dict):
                migrated_items.append(item)
                continue
            try:
                migrated_items.append(normalize_plan_item(item))
            except AgendaContractError:
                migrated_items.append(deepcopy(item))
        if migrated_items != data["daily_plan"]["items"]:
            data["daily_plan"]["items"] = migrated_items
            changed = True
    return data, changed


def agenda_entry_from_plan(plan: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    result = {
        "entry_id": stable_id("agenda_entry", "plan", plan.get("plan_id")),
        "title": _text(plan.get("title") or plan.get("activity"), 240),
        "kind": "planned",
        "status": _normalize_status(plan.get("status"), "planned"),
        "source_kind": "planned",
        "evidence_level": normalize_evidence_level(plan.get("evidence_level"), "L0"),
        "visibility": _text(plan.get("visibility"), 32) or "private",
        "certainty": _text(plan.get("certainty"), 24) or "medium",
        "source_refs": _list(plan.get("source_refs")) or [_text(plan.get("plan_id"), 120)],
    }
    if reason:
        result["reconciliation_reason"] = reason
    for key in ("plan_id", "start_at", "end_at", "date", "time", "end", "participants", "version"):
        if key in plan:
            result[key] = deepcopy(plan[key])
    return result


def agenda_entry_from_activity(activity: dict[str, Any], *, source_refs: Iterable[str] = ()) -> dict[str, Any]:
    refs = _list(list(source_refs)) or _list(activity.get("source_refs")) or [_text(activity.get("activity_id"), 120)]
    result = {
        "entry_id": stable_id("agenda_entry", "activity", activity.get("activity_id")),
        "title": _text(activity.get("title"), 240),
        "kind": "observed",
        "status": _normalize_status(activity.get("status"), "active"),
        "source_kind": "observed",
        "evidence_level": normalize_evidence_level(activity.get("evidence_level"), "L2"),
        "visibility": _text(activity.get("visibility"), 32) or "private",
        "certainty": _text(activity.get("certainty"), 24) or "medium",
        "source_refs": refs,
    }
    for key in ("activity_id", "start_at", "end_at", "participants", "version", "kind"):
        if key in activity:
            result[key] = deepcopy(activity[key])
    return result

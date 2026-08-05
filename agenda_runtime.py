# -*- coding: utf-8 -*-
"""Pure local-data runtime mixin for the chat-side C3 agenda."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

try:
    from .activity_capture import ActivityCapture
    from .agenda_contracts import (
        SCHEDULE_WINDOWS,
        interval_overlaps_window,
        migrate_store,
        normalize_observed_activity,
        normalize_plan_item,
        normalize_reconciliation,
        normalize_window_snapshot,
        stable_id,
        window_bounds,
    )
    from .schedule_reconciler import reconcile
    from .unified_agenda import build_unified_agenda, format_agenda_context
except ImportError:
    from activity_capture import ActivityCapture
    from agenda_contracts import (
        SCHEDULE_WINDOWS,
        interval_overlaps_window,
        migrate_store,
        normalize_observed_activity,
        normalize_plan_item,
        normalize_reconciliation,
        normalize_window_snapshot,
        stable_id,
        window_bounds,
    )
    from schedule_reconciler import reconcile
    from unified_agenda import build_unified_agenda, format_agenda_context


class AgendaRuntimeMixin:
    """Keep C3 state in ``self.data`` and nowhere else."""

    def _agenda_timezone_name(self) -> str:
        getter = getattr(self, "_calendar_timezone_name", None)
        if callable(getter):
            try:
                return str(getter() or "Asia/Shanghai")
            except Exception:
                pass
        return str(getattr(self, "calendar_timezone", "Asia/Shanghai") or "Asia/Shanghai")

    def _agenda_now(self) -> datetime:
        getter = getattr(self, "_calendar_now", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return datetime.now().astimezone()

    def _agenda_prepare_store(self) -> None:
        if not isinstance(getattr(self, "data", None), dict):
            self.data = {}
        migrated, changed = migrate_store(self.data)
        self.data = migrated
        if changed:
            self._agenda_migration_dirty = True
        if not isinstance(getattr(self, "_agenda_capture", None), ActivityCapture):
            self._agenda_capture = ActivityCapture()

    def _agenda_activities_store(self) -> list[dict[str, Any]]:
        self._agenda_prepare_store()
        return self.data["observed_activities"]

    def _agenda_snapshots_store(self) -> list[dict[str, Any]]:
        self._agenda_prepare_store()
        return self.data["window_snapshots"]

    def _agenda_reconciliation_store(self) -> list[dict[str, Any]]:
        self._agenda_prepare_store()
        return self.data["agenda_reconciliation_history"]

    def _agenda_capture_inbound_message(
        self,
        *,
        text: str,
        event_time: datetime,
        source_ref: str,
        conversation_id: str,
        participant: str = "user",
        message_count: int = 1,
        topic: str = "",
        visibility: str = "private",
    ) -> dict[str, Any] | None:
        self._agenda_prepare_store()
        candidate = self._agenda_capture.capture_message(
            text=text,
            event_time=event_time,
            source_ref=source_ref,
            conversation_id=conversation_id,
            participant=participant,
            message_count=message_count,
            topic=topic,
            visibility=visibility,
        )
        if candidate is None:
            return None
        activities = self._agenda_activities_store()
        activity_id = candidate.get("activity_id")
        existing = next((item for item in activities if item.get("activity_id") == activity_id), None)
        if existing is None:
            activities.append(deepcopy(candidate))
            result = candidate
        else:
            existing_refs = list(existing.get("source_refs") or [])
            for ref in candidate.get("source_refs") or []:
                if ref not in existing_refs:
                    existing_refs.append(ref)
            existing.update(deepcopy(candidate))
            existing["source_refs"] = existing_refs[:50]
            existing["version"] = int(existing.get("version") or 1) + 1
            result = deepcopy(existing)
        activities[:] = activities[-500:]
        return result

    def _agenda_capture_hard_fact(self, activity: dict[str, Any]) -> dict[str, Any]:
        self._agenda_prepare_store()
        normalized = normalize_observed_activity(activity, now=self._agenda_now())
        activities = self._agenda_activities_store()
        existing = next((item for item in activities if item.get("activity_id") == normalized.get("activity_id")), None)
        if existing is None:
            activities.append(normalized)
            result = deepcopy(normalized)
        else:
            existing.update(deepcopy(normalized))
            existing["version"] = int(existing.get("version") or 1) + 1
            result = deepcopy(existing)
        activities[:] = activities[-500:]
        return result

    def _agenda_current_plan_items(self) -> list[dict[str, Any]]:
        self._agenda_prepare_store()
        plan = self.data.get("daily_plan") if isinstance(self.data.get("daily_plan"), dict) else {}
        plan_date = str(plan.get("date") or self._agenda_now().date().isoformat())[:10]
        items = plan.get("items") if isinstance(plan.get("items"), list) else []
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            item.setdefault("date", plan_date)
            try:
                normalized = normalize_plan_item(item, plan_id=str(item.get("plan_id") or f"{plan_date}:{index}"), now=self._agenda_now())
            except Exception:
                continue
            if normalized.get("start_at") is None:
                clock = str(normalized.get("time") or normalized.get("start") or "").strip()
                if clock:
                    normalized["start_at"] = f"{plan_date}T{clock}:00" if len(clock) == 5 else f"{plan_date}T{clock}"
            if normalized.get("end_at") is None:
                end_clock = str(normalized.get("end") or normalized.get("end_time") or "").strip()
                if end_clock:
                    normalized["end_at"] = f"{plan_date}T{end_clock}:00" if len(end_clock) == 5 else f"{plan_date}T{end_clock}"
            result.append(normalized)
        return result

    def _agenda_build(self, *, date_key: str = "") -> dict[str, Any]:
        self._agenda_prepare_store()
        return build_unified_agenda(
            plans=self._agenda_current_plan_items(),
            activities=self._agenda_activities_store(),
            now=self._agenda_now(),
            date_key=date_key,
            timezone_name=self._agenda_timezone_name(),
        )

    def _agenda_context_for_prompt(self, *, max_entries: int = 8) -> str:
        return format_agenda_context(self._agenda_build(), max_entries=max_entries)

    def _agenda_snapshot_window(
        self,
        *,
        date_key: str,
        window: str,
        open_items: list[str] | None = None,
    ) -> dict[str, Any]:
        self._agenda_prepare_store()
        timezone_name = self._agenda_timezone_name()
        start, end = window_bounds(date_key, window, timezone_name=timezone_name)
        plans = [item for item in self._agenda_current_plan_items() if interval_overlaps_window(item, start, end, timezone_name=timezone_name)]
        activities = [item for item in self._agenda_activities_store() if interval_overlaps_window(item, start, end, timezone_name=timezone_name)]
        now = self._agenda_now()
        settled = reconcile(plans, activities, now=now)
        snapshot_id = f"agenda_snapshot:{date_key}:{window}"
        snapshot = normalize_window_snapshot(
            {
                "snapshot_id": snapshot_id,
                "date": date_key,
                "window_date": date_key,
                "window": window,
                "start_at": start.isoformat(timespec="seconds"),
                "end_at": end.isoformat(timespec="seconds"),
                "timezone": timezone_name,
                "planned": settled["plans"],
                "observed": settled["activities"],
                "reconciled": settled["reconciliations"],
                "open_items": list(open_items or []),
                "source_refs": [str(item.get("activity_id")) for item in settled["activities"] if item.get("activity_id")],
                "certainty": "high" if settled["reconciliations"] else "medium",
            },
            now=now,
        )
        snapshots = self._agenda_snapshots_store()
        existing = next((item for item in snapshots if item.get("snapshot_id") == snapshot_id), None)
        if existing is None:
            snapshots.append(snapshot)
        else:
            comparable_old = {key: value for key, value in existing.items() if key not in {"generated_at", "version"}}
            comparable_new = {key: value for key, value in snapshot.items() if key not in {"generated_at", "version"}}
            if comparable_old != comparable_new:
                snapshot["version"] = int(existing.get("version") or 1) + 1
                existing.clear()
                existing.update(snapshot)
            else:
                snapshot = deepcopy(existing)
        snapshots[:] = snapshots[-240:]

        reconciliation = normalize_reconciliation(
            {
                "reconciliation_id": f"reconciliation:{date_key}:{window}",
                "date": date_key,
                "window_date": date_key,
                "window": window,
                "start_at": snapshot.get("start_at"),
                "end_at": snapshot.get("end_at"),
                "timezone": timezone_name,
                "plans": settled["reconciliations"],
                "observed_activity_ids": list(snapshot.get("source_refs") or []),
                "source_refs": [snapshot_id],
                "status": "completed",
            },
            now=now,
        )
        history = self._agenda_reconciliation_store()
        old_record = next((item for item in history if item.get("reconciliation_id") == reconciliation["reconciliation_id"]), None)
        if old_record is None:
            history.append(reconciliation)
        else:
            old_record.update(reconciliation)
            reconciliation = deepcopy(old_record)
        history[:] = history[-480:]
        return snapshot

    def _agenda_closed_windows(self, now: datetime) -> list[tuple[str, str]]:
        self._agenda_prepare_store()
        timezone_name = self._agenda_timezone_name()
        local_now = now
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=window_bounds(local_now.date(), "morning", timezone_name=timezone_name)[0].tzinfo)
        existing = {str(item.get("snapshot_id")) for item in self._agenda_snapshots_store() if isinstance(item, dict)}
        candidates: list[tuple[str, str]] = []
        for offset in range(-3, 2):
            target = (local_now + timedelta(days=offset)).date()
            for slug, _name, _start, _end in SCHEDULE_WINDOWS:
                _window_start, window_end = window_bounds(target, slug, timezone_name=timezone_name)
                snapshot_id = f"agenda_snapshot:{target.isoformat()}:{slug}"
                if window_end <= local_now and snapshot_id not in existing:
                    candidates.append((target.isoformat(), slug))
        return candidates

    def _agenda_maintenance_tick(self) -> list[dict[str, Any]]:
        self._agenda_prepare_store()
        settled: list[dict[str, Any]] = []
        now = self._agenda_now()
        for date_key, window in self._agenda_closed_windows(now):
            settled.append(self._agenda_snapshot_window(date_key=date_key, window=window))
        return settled

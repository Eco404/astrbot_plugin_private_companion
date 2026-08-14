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
    from .agenda_disclosure_policy import AgendaDisclosurePolicy
    from .runtime_scene_resolver import RuntimeSceneResolver
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
    from agenda_disclosure_policy import AgendaDisclosurePolicy
    from runtime_scene_resolver import RuntimeSceneResolver


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
        bot_id = str(
            getattr(self, "bot_id", "")
            or getattr(self, "bot_personal_subject", "")
            or "bot_self"
        ).strip()
        timezone_name = self._agenda_timezone_name()
        policy = getattr(self, "_agenda_disclosure_policy", None)
        if not isinstance(policy, AgendaDisclosurePolicy) or policy.bot_id != bot_id or policy.timezone_name != timezone_name:
            self._agenda_disclosure_policy = AgendaDisclosurePolicy(bot_id=bot_id, timezone_name=timezone_name)
        runtime_resolver = getattr(self, "_runtime_scene_resolver", None)
        if (
            not isinstance(runtime_resolver, RuntimeSceneResolver)
            or runtime_resolver.bot_id != bot_id
            or getattr(runtime_resolver, "timezone_name", timezone_name) != timezone_name
        ):
            self._runtime_scene_resolver = RuntimeSceneResolver(
                bot_id=bot_id,
                clock=self._agenda_now,
                timezone_name=timezone_name,
            )

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
        payload = dict(activity or {})
        payload.setdefault("actor_type", "bot")
        payload.setdefault("subject_actor_id", self._agenda_disclosure_policy.bot_id)
        payload.setdefault("source_actor_id", "system")
        normalized = normalize_observed_activity(payload, now=self._agenda_now())
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
            item.setdefault("subject_actor_id", getattr(getattr(self, "_agenda_disclosure_policy", None), "bot_id", "bot_self"))
            item.setdefault("actor_type", "bot")
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

    def _agenda_disclosure_view(
        self,
        purpose: str = "future_schedule",
        *,
        now: datetime | None = None,
        target_user_id: str = "",
        max_entries: int = 32,
        date_key: str = "",
    ) -> dict[str, Any]:
        """Return the only agenda view that should cross a module boundary."""

        self._agenda_prepare_store()
        agenda = self._agenda_build(date_key=str(date_key or ""))
        return self._agenda_disclosure_policy.build_view(
            agenda,
            now=now or self._agenda_now(),
            purpose=purpose,
            target_user_id=target_user_id,
            max_entries=max_entries,
        )

    def _agenda_runtime_scene(
        self,
        *,
        conversation_state: Any = None,
        now: datetime | None = None,
        hard_constraints: Any = None,
    ) -> dict[str, Any] | None:
        """Resolve a short-lived Bot-only current state without mutating plans."""

        self._agenda_prepare_store()
        agenda = self._agenda_build()
        # A clock window or a soft plan is not a runtime state.  The resolver
        # may only consume entries that the disclosure layer has already
        # qualified as a Bot current fact.  This prevents ordinary planned
        # activity text (for example, "上课" or "出门") from becoming a
        # short-lived ``self_state_commit`` merely because its time arrived.
        bot_id = str(getattr(self._agenda_disclosure_policy, "bot_id", "bot_self") or "bot_self")
        candidates: list[dict[str, Any]] = []
        for item in (
            list(agenda.get("current_fact") or [])
            + list(agenda.get("plans") or [])
        ):
            if not isinstance(item, dict):
                continue
            if str(item.get("subject_actor_id") or "") != bot_id:
                continue
            phase = str(item.get("temporal_phase") or "").lower()
            eligibility = str(item.get("fact_eligibility") or "").lower()
            if phase != "current" or eligibility not in {"current_internal", "current_observed"}:
                continue
            candidates.append(item)
        return self._runtime_scene_resolver.resolve_now(
            candidates,
            conversation_state=conversation_state,
            hard_constraints=hard_constraints,
            now=now or self._agenda_now(),
        )

    @staticmethod
    def _agenda_clock_from_value(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) >= 16 and "T" in text:
            return text.split("T", 1)[1][:5]
        return text[:5] if len(text) >= 5 and text[2:3] == ":" else ""

    def _agenda_current_context_item(
        self,
        *,
        conversation_state: Any = None,
        now: datetime | None = None,
        hard_constraints: Any = None,
    ) -> dict[str, Any] | None:
        """Return a Bot-only current item for behavior and scene consumers.

        The result is either an evidence-backed ``current_fact`` or a
        short-lived runtime commit.  Raw plan prose and future commitments are
        deliberately never returned from this boundary.
        """

        current = now or self._agenda_now()
        view = self._agenda_disclosure_view("current_fact", now=current, max_entries=32)
        entries = getattr(view, "entries", None)
        if entries is None and hasattr(view, "get"):
            try:
                entries = view.get("entries", [])
            except Exception:
                entries = []
        bot_id = str(getattr(self._agenda_disclosure_policy, "bot_id", "bot_self") or "bot_self")
        eligible = [
            item
            for item in entries
            if isinstance(item, dict)
            and str(item.get("subject_actor_id") or "") == bot_id
            and str(item.get("temporal_phase") or "").lower() == "current"
            and str(item.get("fact_eligibility") or "").lower() in {"current_internal", "current_observed"}
        ]
        if eligible:
            selected = sorted(
                eligible,
                key=lambda item: (
                    str(item.get("fact_eligibility") or "") != "current_observed",
                    str(item.get("start_at") or item.get("committed_at") or ""),
                ),
            )[0]
            title = str(selected.get("title") or selected.get("state") or selected.get("activity") or "").strip()[:120]
            return {
                **deepcopy(selected),
                "time": str(selected.get("time") or self._agenda_clock_from_value(selected.get("start_at") or selected.get("committed_at")))[:12],
                "end": str(selected.get("end") or self._agenda_clock_from_value(selected.get("end_at") or selected.get("valid_until")))[:12],
                "activity": title,
                "title": title,
                "message_seed": "",
            }

        runtime = self._agenda_runtime_scene(
            conversation_state=conversation_state,
            hard_constraints=hard_constraints,
            now=current,
        )
        if not isinstance(runtime, dict):
            return None
        title = str(runtime.get("state") or runtime.get("title") or "").strip()[:120]
        if not title:
            return None
        return {
            **deepcopy(runtime),
            "time": self._agenda_clock_from_value(runtime.get("committed_at")),
            "end": self._agenda_clock_from_value(runtime.get("valid_until")),
            "activity": title,
            "title": title,
            "mood": "当前状态",
            "message_seed": "",
        }

    def _agenda_context_for_prompt(self, *, max_entries: int = 8) -> str:
        # Prompt consumers receive a filtered future view; diagnostics remain
        # available through ``_agenda_disclosure_view('diagnostic')`` only.
        view = self._agenda_disclosure_view("future_schedule", max_entries=max_entries)
        return format_agenda_context({"entries": view.get("entries", []), "date": self._agenda_now().date().isoformat()}, max_entries=max_entries)

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
                "subject_actor_id": self._agenda_disclosure_policy.bot_id,
                "actor_type": "bot",
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
                "status": "reconciled",
                "subject_actor_id": self._agenda_disclosure_policy.bot_id,
                "actor_type": "bot",
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

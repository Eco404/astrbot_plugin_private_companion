from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "c3_outbox_companion"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

from c3_outbox_companion.bot_personal_outbox import BotPersonalOutbox


def run(coro):
    return asyncio.run(coro)


def payload():
    return {"date": "2026-07-30", "window": "evening", "summary": "整理项目", "items": ["测试"]}


def test_enqueue_is_local_first_and_idempotent():
    data = {}
    saved = []
    clock = [100.0]
    outbox = BotPersonalOutbox(data, save=lambda: saved.append(True), clock=lambda: clock[0])

    first = run(outbox.enqueue(
        memory_type="bot_schedule_plan",
        payload=payload(),
        idempotency_key="daily_plan:2026-07-30",
        occurred_at="2026-07-30T19:00:00+08:00",
    ))
    duplicate = run(outbox.enqueue(
        memory_type="bot_schedule_plan",
        payload=payload(),
        idempotency_key="daily_plan:2026-07-30",
        occurred_at="2026-07-30T19:00:00+08:00",
    ))

    assert first["state"] == "pending"
    assert duplicate["deduplicated"] is True
    assert len(data["bot_personal_outbox"]) == 1
    assert saved


def test_drain_success_does_not_duplicate_remote_write():
    data = {}
    outbox = BotPersonalOutbox(data, clock=lambda: 100.0)
    calls = []

    async def sender(envelope):
        calls.append(envelope["idempotency_key"])
        return {"ok": True, "record_id": "remote-1", "version": 1, "state": "sent"}

    run(outbox.enqueue(
        memory_type="bot_window_snapshot",
        payload=payload(),
        idempotency_key="agenda_snapshot:2026-07-30:evening",
        occurred_at="2026-07-30T19:00:00+08:00",
    ))
    result = run(outbox.drain(sender, now=100.0))
    again = run(outbox.drain(sender, now=100.0))

    assert result[0]["state"] == "sent"
    assert again == []
    assert calls == ["agenda_snapshot:2026-07-30:evening"]
    assert outbox.status()["sent"] == 1


def test_retry_backoff_and_dead_letter_are_deterministic():
    data = {}
    outbox = BotPersonalOutbox(data, clock=lambda: 100.0, max_attempts=2, base_backoff_seconds=10.0)

    async def sender(_envelope):
        return {"ok": False, "state": "degraded", "error_code": "bridge_timeout"}

    run(outbox.enqueue(
        memory_type="bot_daily_diary",
        payload={"date": "2026-07-30", "summary": "今天完成了回归"},
        idempotency_key="diary:2026-07-30",
        occurred_at="2026-07-30T23:00:00+08:00",
    ))
    first = run(outbox.drain(sender, now=100.0))
    not_due = run(outbox.drain(sender, now=109.9))
    last = run(outbox.drain(sender, now=110.0))

    assert first[0]["state"] == "retry"
    assert not_due == []
    assert last[0]["state"] == "dead_letter"
    assert outbox.status()["dead_letter"] == 1


def test_newer_version_replaces_pending_and_same_version_conflict_is_rejected():
    data = {}
    outbox = BotPersonalOutbox(data, clock=lambda: 100.0)
    kwargs = {
        "memory_type": "bot_schedule_reconciliation",
        "idempotency_key": "reconciliation:2026-07-30:evening",
        "occurred_at": "2026-07-30T22:00:00+08:00",
    }
    first = run(outbox.enqueue(payload={**payload(), "summary": "old"}, version=1, **kwargs))
    conflict = run(outbox.enqueue(payload={**payload(), "summary": "conflict"}, version=1, **kwargs))
    newer = run(outbox.enqueue(payload={**payload(), "summary": "new"}, version=2, **kwargs))
    stale = run(outbox.enqueue(payload={**payload(), "summary": "stale"}, version=1, **kwargs))

    assert first["state"] == "pending"
    assert conflict["state"] == "version_conflict"
    assert newer["state"] == "pending"
    assert stale["state"] == "stale_version"
    assert data["bot_personal_outbox"][0]["version"] == 2

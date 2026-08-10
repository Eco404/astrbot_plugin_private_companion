from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from identity_namespace import NamespaceContext
from migration_outbox import (
    MigrationOutbox,
    OutboxConflict,
    OutboxError,
    RevisionGap,
    StaleMigrationEpoch,
)


EPOCH = "req041-20260810-001"
POLICY = "req041-v1"


def _context(**changes: str) -> NamespaceContext:
    values = {
        "kind": "private",
        "identity_id": "person_aaaaaaaaaaaaaaaaaaaaaaaa",
        "group_id": "",
        "assurance": "verified",
        "profile_status": "active",
        "policy_version": POLICY,
        "migration_epoch": EPOCH,
    }
    values.update(changes)
    return NamespaceContext(**values)


class MigrationOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "req041.sqlite3"
        self.outbox = MigrationOutbox(self.path, clock=lambda: 100.0)
        self.outbox.begin_epoch(EPOCH, policy_version=POLICY)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _enqueue(self, *, event: str = "event-1", revision: int = 1, payload=None) -> str:
        return self.outbox.enqueue(
            event_id=event,
            source_revision=revision,
            namespace=_context(),
            migration_epoch=EPOCH,
            policy_version=POLICY,
            payload=payload or {"operation": "identity_shadow_upsert", "identity_ref": "person-a"},
        )

    def test_enqueue_is_durable_and_idempotent_across_restart(self) -> None:
        self.assertEqual("enqueued", self._enqueue())
        self.assertEqual("duplicate", self._enqueue())
        reopened = MigrationOutbox(self.path, clock=lambda: 101.0)
        pending = reopened.pending(EPOCH)
        self.assertEqual(1, len(pending))
        self.assertEqual("event-1", pending[0].event_id)
        self.assertEqual("person-a", pending[0].payload["identity_ref"])
        reopened.mark_applied("event-1", EPOCH, target_revision=1)
        self.assertEqual([], reopened.pending(EPOCH))

    def test_same_event_with_different_payload_is_conflict(self) -> None:
        self._enqueue()
        with self.assertRaises(OutboxConflict):
            self._enqueue(payload={"operation": "identity_shadow_upsert", "identity_ref": "person-b"})

    def test_payload_rejects_raw_conversation_material(self) -> None:
        with self.assertRaisesRegex(OutboxError, "outbox_payload_invalid"):
            self._enqueue(payload={"raw_text": "private message"})

    def test_namespace_epoch_and_policy_must_match(self) -> None:
        with self.assertRaisesRegex(StaleMigrationEpoch, "outbox_namespace_epoch_mismatch"):
            self.outbox.enqueue(
                event_id="event-x",
                source_revision=1,
                namespace=_context(migration_epoch="old-epoch"),
                migration_epoch=EPOCH,
                policy_version=POLICY,
                payload={"operation": "noop"},
            )
        self.outbox.set_epoch_state(EPOCH, "verified", checkpoint="done")
        with self.assertRaisesRegex(StaleMigrationEpoch, "migration_epoch_closed"):
            self._enqueue(event="event-closed")

    def test_failed_event_is_retriable_and_preserves_count(self) -> None:
        self._enqueue()
        self.outbox.mark_failed("event-1", EPOCH, error_code="target_timeout")
        item = self.outbox.pending(EPOCH)[0]
        self.assertEqual("failed", item.state)
        self.assertEqual(1, item.retry_count)
        self.assertEqual("target_timeout", item.error_code)

    def test_revision_sequence_rejects_gap_and_accepts_duplicate(self) -> None:
        self.assertEqual("advanced", self.outbox.advance_revision("identity:person-a", EPOCH, expected=0, target=1))
        self.assertEqual("duplicate", self.outbox.advance_revision("identity:person-a", EPOCH, expected=0, target=1))
        with self.assertRaises(RevisionGap):
            self.outbox.advance_revision("identity:person-a", EPOCH, expected=2, target=3)
        self.assertEqual("advanced", self.outbox.advance_revision("identity:person-a", EPOCH, expected=1, target=2))

    def test_enqueue_next_allocates_revision_and_event_in_one_transaction(self) -> None:
        first = self.outbox.enqueue_next(
            stream_key="relationship:person-a",
            event_id="relationship-event-1",
            namespace=_context(),
            migration_epoch=EPOCH,
            policy_version=POLICY,
            payload={"operation": "relationship_event", "applied_delta": 2},
        )
        replay = self.outbox.enqueue_next(
            stream_key="relationship:person-a",
            event_id="relationship-event-1",
            namespace=_context(),
            migration_epoch=EPOCH,
            policy_version=POLICY,
            payload={"operation": "relationship_event", "applied_delta": 2},
        )
        second = self.outbox.enqueue_next(
            stream_key="relationship:person-a",
            event_id="relationship-event-2",
            namespace=_context(),
            migration_epoch=EPOCH,
            policy_version=POLICY,
            payload={"operation": "relationship_event", "applied_delta": -1},
        )
        self.assertEqual({"status": "enqueued", "source_revision": 1}, first)
        self.assertEqual({"status": "duplicate", "source_revision": 1}, replay)
        self.assertEqual({"status": "enqueued", "source_revision": 2}, second)
        self.assertEqual(2, self.outbox.stream_revision("relationship:person-a", EPOCH))
        self.assertEqual([1, 2], [item.source_revision for item in self.outbox.pending(EPOCH)])

    def test_enqueue_next_conflict_does_not_consume_revision(self) -> None:
        kwargs = {
            "stream_key": "relationship:person-a",
            "event_id": "relationship-event-1",
            "namespace": _context(),
            "migration_epoch": EPOCH,
            "policy_version": POLICY,
        }
        self.outbox.enqueue_next(**kwargs, payload={"operation": "relationship_event", "applied_delta": 1})
        with self.assertRaises(OutboxConflict):
            self.outbox.enqueue_next(**kwargs, payload={"operation": "relationship_event", "applied_delta": 9})
        self.assertEqual(1, self.outbox.stream_revision("relationship:person-a", EPOCH))

    def test_enqueue_next_expected_revision_gap_rolls_back_event_and_revision(self) -> None:
        with self.assertRaises(RevisionGap):
            self.outbox.enqueue_next(
                stream_key="relationship:person-a",
                event_id="relationship-event-gap",
                namespace=_context(),
                migration_epoch=EPOCH,
                policy_version=POLICY,
                payload={"operation": "relationship_event", "applied_delta": 1},
                expected_source_revision=2,
            )
        self.assertEqual(0, self.outbox.stream_revision("relationship:person-a", EPOCH))
        self.assertEqual([], self.outbox.pending(EPOCH))

    def test_tombstone_is_durable_idempotent_and_conflict_safe(self) -> None:
        self.assertEqual("created", self.outbox.add_tombstone("identity:person-a", EPOCH, revision=2, reason_code="unlink"))
        self.assertEqual("duplicate", self.outbox.add_tombstone("identity:person-a", EPOCH, revision=2, reason_code="unlink"))
        self.assertEqual("advanced", self.outbox.add_tombstone("identity:person-a", EPOCH, revision=3, reason_code="delete"))
        with self.assertRaises(OutboxConflict):
            self.outbox.add_tombstone("identity:person-a", EPOCH, revision=2, reason_code="unlink")
        reopened = MigrationOutbox(self.path)
        self.assertEqual(3, reopened.tombstone("identity:person-a", EPOCH)["revision"])

    def test_enqueue_next_with_tombstone_is_atomic_and_replay_safe(self) -> None:
        kwargs = {
            "stream_key": "identity:person-a",
            "event_id": "unlink-event-1",
            "namespace": _context(),
            "migration_epoch": EPOCH,
            "policy_version": POLICY,
            "payload": {"operation": "identity_unlink", "identity_key_ref": "link-hash"},
            "tombstone_key": "identity-link:link-hash",
            "reason_code": "identity_unlink",
        }
        first = self.outbox.enqueue_next_with_tombstone(**kwargs)
        replay = self.outbox.enqueue_next_with_tombstone(**kwargs)
        self.assertEqual({"status": "enqueued", "source_revision": 1}, first)
        self.assertEqual({"status": "duplicate", "source_revision": 1}, replay)
        self.assertEqual(1, self.outbox.stream_revision("identity:person-a", EPOCH))
        self.assertEqual(1, self.outbox.tombstone("identity-link:link-hash", EPOCH)["revision"])

    def test_epoch_checkpoint_survives_restart(self) -> None:
        state = self.outbox.set_epoch_state(EPOCH, "replaying", checkpoint="identity:42")
        self.assertEqual("identity:42", state["checkpoint"])
        reopened = MigrationOutbox(self.path)
        self.assertEqual("replaying", reopened.epoch_status(EPOCH)["state"])
        self.assertEqual("identity:42", reopened.epoch_status(EPOCH)["checkpoint"])


if __name__ == "__main__":
    unittest.main()

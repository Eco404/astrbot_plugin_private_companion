from __future__ import annotations

from copy import deepcopy
import unittest

from migration_scoped_projection import ScopedProjectionSynchronizer
from unified_person_registry import UnifiedPersonRegistry


def _identity(subject: str = "10001") -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1",
        "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user",
        "platform_subject_id": subject,
    }


class _Remote:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict] = {}

    @staticmethod
    def _key(context, kind: str, record_id: str) -> tuple[str, str, str]:
        return context.cache_scope(), kind, record_id

    def read(self, context, *, record_kind: str, record_id: str):
        row = self.rows.get(self._key(context, record_kind, record_id))
        return {"ok": True, "code": "found" if row else "not_found", "record": deepcopy(row)}

    def list_records(self, context, *, record_kind: str, limit: int = 100):
        scope = context.cache_scope()
        records = [deepcopy(row) for (stored_scope, kind, _), row in self.rows.items() if stored_scope == scope and kind == record_kind]
        return {"ok": True, "code": "listed", "records": records[:limit]}

    def upsert(self, context, *, record_kind: str, record_id: str, revision: int, payload: dict, event_id: str):
        key = self._key(context, record_kind, record_id)
        row = self.rows.get(key)
        expected = int(row.get("revision") or 0) + 1 if row else 1
        if revision != expected:
            return {"ok": False, "code": "revision_gap"}
        self.rows[key] = {
            "record_id": record_id, "record_kind": record_kind, "revision": revision,
            "payload": deepcopy(payload), "event_id": event_id,
        }
        return {"ok": True, "code": "updated" if row else "created"}

    def tombstone(self, context, *, record_kind: str, record_id: str, revision: int, event_id: str):
        key = self._key(context, record_kind, record_id)
        row = self.rows.get(key)
        if not row or revision != int(row["revision"]) + 1:
            return {"ok": False, "code": "revision_gap"}
        self.rows.pop(key)
        return {"ok": True, "code": "tombstoned"}


class ScopedProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.remote = _Remote()
        self.sync = ScopedProjectionSynchronizer(
            read=self.remote.read, list_records=self.remote.list_records,
            upsert=self.remote.upsert, tombstone=self.remote.tombstone,
            migration_epoch="epoch-1", policy_version="req041-v1",
        )
        self.snapshot: dict = {}
        created = UnifiedPersonRegistry(self.snapshot).create_or_link(
            _identity(), profile={"display_name": "A"}, operation_id="create-a"
        )
        self.person_id = created["person_id"]
        self.snapshot["users"] = {
            "10001": {
                "user_id": "10001", "identity_subject_id": "10001", "unified_person_id": self.person_id,
                "nickname": "private-name", "style": "private-style",
                "companion_memory": {"items": [{"text": "private-sentinel"}]},
                "dialogue_episodes": [{"summary": "private-episode"}],
                "expression_profile": {
                    "learned_rules": [{"id": "approved", "style": "private-rule"}],
                    "pending_rules": [{"id": "pending", "style": "candidate"}],
                    "samples": ["private-evidence"],
                },
                "relationship_role": "owner", "relationship_score": 99,
            }
        }
        self.snapshot["groups"] = {
            "group-a": {
                "group_id": "group-a", "recent_messages": [{"text": "group-a-sentinel"}],
                "group_episodes": [{"summary": "group-a-episode"}],
                "expression_profile": {"learned_rules": [{"id": "ga", "style": "group-a-rule"}]},
                "members": {"10001": {"name": "A-in-group-a", "count": 3, "recent_phrases": ["ga-phrase"]}},
            },
            "group-b": {
                "group_id": "group-b", "recent_messages": [{"text": "group-b-sentinel"}],
                "members": {"10001": {"name": "A-in-group-b", "count": 1, "recent_phrases": ["gb-phrase"]}},
            },
        }

    def test_builds_private_group_shared_and_group_member_without_privilege_projection(self) -> None:
        records, contexts = self.sync.build_records(self.snapshot)
        self.assertTrue(any(item.context.kind == "private" for item in records))
        group_scopes = {item.context.group_id for item in records if item.context.kind == "group_shared"}
        self.assertEqual(2, len(group_scopes))
        member_scopes = {item.context.group_id for item in records if item.context.kind == "group_member"}
        self.assertEqual(group_scopes, member_scopes)
        serialized = str([item.payload for item in records])
        self.assertIn("private-sentinel", serialized)
        self.assertIn("group-a-sentinel", serialized)
        self.assertIn("group-b-sentinel", serialized)
        self.assertNotIn("relationship_role", serialized)
        self.assertNotIn("relationship_score", serialized)
        self.assertTrue(all(context.persona_id == "default" for context in contexts))

    def test_sync_is_idempotent_updates_and_tombstones_removed_legacy_fields(self) -> None:
        first = self.sync.sync_snapshot(self.snapshot)
        self.assertTrue(first["ok"])
        self.assertGreater(first["created"], 0)
        second = self.sync.sync_snapshot(deepcopy(self.snapshot))
        self.assertTrue(second["ok"])
        self.assertEqual(0, second["created"])
        self.assertEqual(0, second["updated"])
        self.assertEqual(first["records"], second["unchanged"])
        changed = deepcopy(self.snapshot)
        changed["users"]["10001"]["companion_memory"]["items"][0]["text"] = "private-updated"
        third = self.sync.sync_snapshot(changed)
        self.assertEqual(1, third["updated"])
        changed["users"]["10001"].pop("dialogue_episodes")
        fourth = self.sync.sync_snapshot(changed)
        self.assertEqual(1, fourth["cleared"])
        changed["users"]["10001"]["dialogue_episodes"] = [{"summary": "restored"}]
        fifth = self.sync.sync_snapshot(changed)
        self.assertEqual(1, fifth["updated"])

    def test_persona_scopes_are_physically_distinct_and_global_rules_are_not_inferred(self) -> None:
        default_records, _ = self.sync.build_records(self.snapshot, source_scope="default")
        persona_records, _ = self.sync.build_records(self.snapshot, source_scope="persona:custom")
        self.assertNotEqual(default_records[0].context.persona_id, persona_records[0].context.persona_id)
        self.assertFalse(any(item.context.kind == "persona_global" for item in default_records + persona_records))

    def test_read_projection_only_opens_after_reconciliation_and_preserves_group_isolation(self) -> None:
        records, _ = self.sync.build_records(self.snapshot)
        private = next(item.context for item in records if item.context.kind == "private")
        group_contexts = {
            item.context.group_id: item.context for item in records if item.context.kind == "group_shared"
        }
        self.assertFalse(self.sync.read_projection(private)["ok"])
        self.sync.sync_snapshot(self.snapshot)
        private_view = self.sync.read_projection(private)
        self.assertTrue(private_view["ok"])
        self.assertEqual("private-name", private_view["fields"]["nickname"])
        self.assertEqual("private-rule", private_view["fields"]["expression_profile"]["learned_rules"][0]["style"])
        observed = {
            context.group_id: self.sync.read_projection(context)["fields"]["recent_messages"][0]["text"]
            for context in group_contexts.values()
        }
        self.assertEqual({"group-a-sentinel", "group-b-sentinel"}, set(observed.values()))
        self.sync.mark_dirty()
        self.assertEqual("scoped_projection_not_reconciled", self.sync.read_projection(private)["code"])

    def test_unlinked_user_and_ambiguous_subject_are_not_projected(self) -> None:
        unlinked = {"users": {"10001": deepcopy(self.snapshot["users"]["10001"])}}
        records, _ = self.sync.build_records(unlinked)
        self.assertEqual([], records)
        duplicated = deepcopy(self.snapshot)
        duplicated["users"]["duplicate"] = deepcopy(duplicated["users"]["10001"])
        records, _ = self.sync.build_records(duplicated)
        self.assertFalse(any(item.context.kind == "private" for item in records))


if __name__ == "__main__":
    unittest.main()

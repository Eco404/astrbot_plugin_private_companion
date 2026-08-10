from __future__ import annotations

import unittest

from identity_namespace import validate_namespace_context
from unified_person_registry import UnifiedPersonRegistry


def _identity(subject_id: str) -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1",
        "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user",
        "platform_subject_id": subject_id,
    }


class IdentityAssuranceNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict = {}
        self.registry = UnifiedPersonRegistry(self.store)

    def _namespace(self, identity: dict[str, str], *, kind: str = "private", group_id: str = ""):
        return self.registry.namespace_context(
            identity,
            kind=kind,
            group_id=group_id,
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
            purpose="memory_read",
        )

    def test_unlinked_complete_identity_is_pending_and_denied(self) -> None:
        result = self._namespace(_identity("10001"))
        self.assertFalse(result["ok"])
        self.assertEqual("namespace_pending_denied", result["code"])
        self.assertEqual("pending", result["context"]["kind"])
        self.assertEqual("unverified", result["context"]["assurance"])
        self.assertEqual([], validate_namespace_context(result["context"]))

    def test_exact_active_link_maps_to_verified_without_mutating_legacy_assurance(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.assertTrue(created["ok"])
        self.assertEqual("observed", created["projection"]["identity_assurance"])
        result = self._namespace(_identity("10001"))
        self.assertTrue(result["ok"])
        self.assertEqual("verified", result["context"]["assurance"])
        self.assertEqual("observed", self.registry.read_projection(created["person_id"])["identity_assurance"])

    def test_explicit_secondary_link_preserves_explicit_assurance(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        linked = self.registry.link_identity(created["person_id"], _identity("10002"), operation_id="link-1")
        self.assertTrue(linked["ok"])
        result = self._namespace(_identity("10002"))
        self.assertTrue(result["ok"])
        self.assertEqual("explicit_linked", result["context"]["assurance"])

    def test_group_member_context_requires_exact_group_and_stays_isolated(self) -> None:
        self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        group_a = self._namespace(_identity("10001"), kind="group_member", group_id="group-a")
        group_b = self._namespace(_identity("10001"), kind="group_member", group_id="group-b")
        self.assertTrue(group_a["ok"])
        self.assertTrue(group_b["ok"])
        self.assertNotEqual(group_a["context"]["group_id"], group_b["context"]["group_id"])
        self.assertEqual(group_a["person_id"], group_b["person_id"])

    def test_same_subject_on_different_bot_does_not_merge(self) -> None:
        first = _identity("10001")
        second = {**first, "bot_account_id": "onebot:bot-2"}
        created = self.registry.create_or_link(first, operation_id="create-1")
        unresolved = self._namespace(second)
        self.assertEqual("pending", unresolved["context"]["kind"])
        self.assertNotEqual(created["person_id"], unresolved["person_id"])

    def test_formal_namespace_for_person_revalidates_primary_exact_link(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        resolved = self.registry.formal_namespace_for_person(
            created["person_id"],
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
        )
        self.assertTrue(resolved["ok"])
        self.assertEqual("verified", resolved["context"]["assurance"])
        root = self.store["unified_person"]
        root["identity_links"][created["identity_key"]]["identity"]["platform_subject_id"] = "tampered"
        rejected = self.registry.formal_namespace_for_person(
            created["person_id"],
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual("identity_exact_link_invalid", rejected["code"])

    def test_formal_person_rejects_unlisted_or_corrupt_secondary_link(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.store["unified_person"]["profiles"][created["person_id"]]["identity_keys"].append("orphan-key")
        result = self.registry.formal_namespace_for_person(
            created["person_id"],
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
        )
        self.assertFalse(result["ok"])
        self.assertEqual("identity_exact_link_invalid", result["code"])

    def test_person_subject_match_accepts_exact_and_scoped_key_only(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.assertTrue(self.registry.matches_person_subject(created["person_id"], "10001"))
        self.assertTrue(self.registry.matches_person_subject(created["person_id"], "onebot:10001:0123456789abcdef"))
        self.assertFalse(self.registry.matches_person_subject(created["person_id"], "other-user"))


if __name__ == "__main__":
    unittest.main()

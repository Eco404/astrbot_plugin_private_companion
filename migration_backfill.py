"""REQ-041 S4 exact-identity and relationship snapshot backfill.

The backfiller consumes an immutable in-memory legacy snapshot.  It never
guesses identity from a user key, nickname, alias, or message content.  A
record is eligible only when it explicitly points at a Unified Person whose
active link contains the complete five-field identity and whose stored
fingerprint can be recomputed exactly.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any

from identity_namespace import NamespaceContext
from migration_coordinator import MigrationCoordinator
from person_context_contract import build_identity_key, canonical_identity
from relationship_account_store import (
    RelationshipAccountStore,
    RelationshipConflict,
    RelationshipNotFound,
    RelationshipStoreError,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _opaque_ref(epoch: str, source_kind: str, source_scope: str, legacy_key: Any) -> str:
    payload = {
        "epoch": epoch,
        "source_kind": source_kind,
        "source_scope": source_scope,
        "legacy_key": str(legacy_key or ""),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _token(value: Any, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        return ""
    return text


def _score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if -1200 <= number <= 1200 else None


class MigrationBackfill:
    """Create local S4 Shadow projections while preserving legacy authority."""

    def __init__(
        self,
        *,
        coordinator: MigrationCoordinator,
        relationship_path: str | Path,
        migration_epoch: str,
        policy_version: str,
    ) -> None:
        self.coordinator = coordinator
        self.migration_epoch = _token(migration_epoch)
        self.policy_version = _token(policy_version, 64)
        if not self.migration_epoch or not self.policy_version:
            raise ValueError("migration_backfill_contract_invalid")
        self.relationships = RelationshipAccountStore(
            relationship_path,
            active_migration_epoch=self.migration_epoch,
        )

    def _pending(self, legacy_key: Any, reason: str, source_scope: str) -> None:
        self.coordinator.record_pending(
            _opaque_ref(self.migration_epoch, "legacy_user", source_scope, legacy_key),
            source_kind="legacy_user",
            reason_code=reason,
        )

    @staticmethod
    def _formal_people(snapshot: dict[str, Any]) -> dict[str, tuple[str, frozenset[tuple[str, str]]]]:
        root = snapshot.get("unified_person")
        if not isinstance(root, dict):
            return {}
        profiles = root.get("profiles") if isinstance(root.get("profiles"), dict) else {}
        links = root.get("identity_links") if isinstance(root.get("identity_links"), dict) else {}
        formal: dict[str, str] = {}
        valid_keys: dict[str, set[str]] = defaultdict(set)
        valid_subjects: dict[str, set[tuple[str, str]]] = defaultdict(set)
        invalid_people: set[str] = set()
        for stored_key, raw_link in links.items():
            if not isinstance(raw_link, dict) or raw_link.get("status") != "active":
                continue
            person_id = _token(raw_link.get("person_id"))
            profile = profiles.get(person_id) if person_id else None
            if (
                not person_id
                or not isinstance(profile, dict)
                or _token(profile.get("person_id")) != person_id
                or str(profile.get("profile_status") or "active") != "active"
            ):
                if person_id:
                    invalid_people.add(person_id)
                continue
            try:
                identity = canonical_identity(raw_link.get("identity"))
                recomputed = build_identity_key(identity)
            except (TypeError, ValueError):
                invalid_people.add(person_id)
                continue
            if _token(stored_key, 160) != recomputed or _token(raw_link.get("identity_key"), 160) != recomputed:
                invalid_people.add(person_id)
                continue
            valid_keys[person_id].add(recomputed)
            valid_subjects[person_id].add(
                (identity["subject_namespace"].split(":", 1)[0], identity["platform_subject_id"])
            )
            assurance = "explicit_linked" if raw_link.get("identity_assurance") == "explicit_linked" else "verified"
            previous = formal.get(person_id)
            if previous == "explicit_linked" or assurance == "explicit_linked":
                formal[person_id] = "explicit_linked"
            else:
                formal[person_id] = assurance
        for person_id in tuple(formal):
            profile = profiles.get(person_id)
            profile_keys = profile.get("identity_keys") if isinstance(profile, dict) else None
            resolved_key = _token(profile.get("resolved_identity_key"), 160) if isinstance(profile, dict) else ""
            if (
                person_id in invalid_people
                or resolved_key not in valid_keys.get(person_id, set())
                or not isinstance(profile_keys, list)
                or any(not isinstance(item, str) for item in profile_keys)
                or set(profile_keys) != valid_keys.get(person_id, set())
            ):
                formal.pop(person_id, None)
        return {
            person_id: (assurance, frozenset(valid_subjects[person_id]))
            for person_id, assurance in formal.items()
        }

    @staticmethod
    def _legacy_record_matches_identity(
        legacy_key: str,
        user: dict[str, Any],
        subjects: frozenset[tuple[str, str]],
    ) -> bool:
        direct = {
            _token(legacy_key, 160),
            _token(user.get("user_id"), 160),
            _token(user.get("identity_subject_id"), 160),
        }
        direct.discard("")
        if any(subject in direct for _platform, subject in subjects):
            return True
        for candidate in direct:
            parts = candidate.rsplit(":", 2)
            if len(parts) != 3 or len(parts[2]) != 16:
                continue
            platform, subject, digest = parts
            if all(char in "0123456789abcdef" for char in digest.lower()) and (platform.lower(), subject) in subjects:
                return True
        return False

    def run(self, legacy_snapshot: dict[str, Any], *, source_scope: str = "default") -> dict[str, Any]:
        if not isinstance(legacy_snapshot, dict):
            raise ValueError("migration_backfill_snapshot_invalid")
        scope = _token(source_scope, 160)
        if not scope:
            raise ValueError("migration_backfill_scope_invalid")
        status = self.coordinator.status()
        if status.get("migration_epoch") != self.migration_epoch or status.get("policy_version") != self.policy_version:
            raise ValueError("migration_backfill_epoch_stale")
        if status.get("phase") not in {"S3", "S4"}:
            raise ValueError("migration_backfill_phase_denied")

        users = legacy_snapshot.get("users") if isinstance(legacy_snapshot.get("users"), dict) else {}
        formal_people = self._formal_people(legacy_snapshot)
        users_by_person: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        pending = 0
        for legacy_key, raw_user in users.items():
            if not isinstance(raw_user, dict):
                self._pending(legacy_key, "legacy_user_invalid", scope)
                pending += 1
                continue
            person_id = _token(raw_user.get("unified_person_id"))
            if not person_id:
                self._pending(legacy_key, "identity_link_missing", scope)
                pending += 1
                continue
            users_by_person[person_id].append((str(legacy_key), raw_user))

        migrated = 0
        idempotent = 0
        conflicts = 0
        for person_id, records in users_by_person.items():
            if len(records) != 1:
                for legacy_key, _ in records:
                    self._pending(legacy_key, "identity_multiple_legacy_records", scope)
                    pending += 1
                conflicts += len(records)
                continue
            legacy_key, user = records[0]
            formal = formal_people.get(person_id)
            if not formal:
                self._pending(legacy_key, "identity_exact_link_invalid", scope)
                pending += 1
                continue
            assurance, subjects = formal
            if not self._legacy_record_matches_identity(legacy_key, user, subjects):
                self._pending(legacy_key, "legacy_identity_subject_mismatch", scope)
                pending += 1
                continue
            score = _score(user.get("relationship_score", 0))
            if score is None:
                self._pending(legacy_key, "relationship_score_invalid", scope)
                pending += 1
                continue
            role = "owner" if str(user.get("relationship_role") or "").strip().lower() == "owner" else "friend"
            raw_mode = str(user.get("relationship_mode") or "normal").strip().lower()
            mode = "owner_exclusive" if role == "owner" and raw_mode == "owner_exclusive" else "normal"
            cap = _token(user.get("relationship_positive_stage_cap_key"), 40) or "deeply_bonded"
            context = NamespaceContext(
                kind="private",
                identity_id=person_id,
                group_id="",
                assurance=assurance,
                profile_status="active",
                policy_version=self.policy_version,
                migration_epoch=self.migration_epoch,
            )
            self.coordinator.register_identity(person_id, assurance=assurance)
            try:
                self.relationships.account(context)
                existed = True
            except RelationshipNotFound:
                existed = False
            operation_digest = hashlib.sha256(
                f"{self.migration_epoch}:{person_id}".encode("utf-8")
            ).hexdigest()[:32]
            try:
                self.relationships.create_account(
                    context,
                    operation_id=f"req041-s4-{operation_digest}",
                    actor="migration",
                    relationship_role=role,
                    relationship_mode=mode,
                    score=score,
                    positive_stage_cap_key=cap,
                    legacy_snapshot=True,
                )
                if existed:
                    idempotent += 1
                else:
                    migrated += 1
                self.coordinator.resolve_pending(
                    _opaque_ref(self.migration_epoch, "legacy_user", scope, legacy_key)
                )
            except RelationshipConflict:
                self._pending(legacy_key, "relationship_snapshot_conflict", scope)
                pending += 1
                conflicts += 1
            except RelationshipStoreError:
                self._pending(legacy_key, "relationship_snapshot_invalid", scope)
                pending += 1
                conflicts += 1

        status = self.coordinator.status()
        if status.get("phase") == "S3":
            status = self.coordinator.transition("S4", checkpoint="identity_relationship_snapshot_backfilled")
        return {
            "phase": status.get("phase", "S4"),
            "migrated": migrated,
            "idempotent": idempotent,
            "pending": pending,
            "conflicts": conflicts,
            "formal_identities": len(formal_people),
            "legacy_users": len(users),
        }


__all__ = ["MigrationBackfill"]

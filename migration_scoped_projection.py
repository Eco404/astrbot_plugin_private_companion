"""REQ-041 profile, memory and learning scoped projection synchronizer."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable

from identity_namespace import NamespaceContext
from scoped_domain_contract import build_scoped_domain_payload
from unified_person_registry import UnifiedPersonRegistry


_PRIVATE_MEMORY_FIELDS = (
    "companion_memory", "intent_profile", "dialogue_episodes", "open_loops",
    "behavior_habits", "action_preferences", "action_consequences", "state_continuity",
    "recent_reply_topics",
)
_GROUP_MEMORY_FIELDS = (
    "recent_messages", "slang_terms", "slang_meanings", "topic_signatures", "topic_threads",
    "group_episodes", "relationship_edges", "atmosphere", "interjection_feedback",
)
_MEMBER_PROFILE_FIELDS = (
    "name", "identity_name", "group_role", "group_role_label", "count", "last_seen",
    "display_name_events",
)
_RULE_EVIDENCE_FIELDS = (
    "samples", "pending_samples", "scene_profiles", "recent_phrases", "endings", "expression_rules",
)
_RECORD_PREFIX = "req041-"


class ScopedProjectionError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _persona_ref(source_scope: str) -> str:
    value = str(source_scope or "").strip()
    if value == "default":
        return "default"
    return "persona-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _group_ref(persona_id: str, group_id: Any) -> str:
    raw = str(group_id or "").strip()
    if not raw:
        return ""
    return "group-" + hashlib.sha256(f"{persona_id}:{raw}".encode("utf-8")).hexdigest()[:32]


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth > 7:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return value[:8000]
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1) for item in value[-256:]]
    if isinstance(value, dict):
        return {
            str(key)[:96]: _bounded(item, depth=depth + 1)
            for key, item in list(value.items())[:256]
            if isinstance(key, str)
        }
    return None


@dataclass(frozen=True, slots=True)
class ScopedProjectionRecord:
    context: NamespaceContext
    record_kind: str
    record_id: str
    payload: dict[str, Any]


class ScopedProjectionSynchronizer:
    """Build and synchronize bounded legacy projections without changing legacy data."""

    def __init__(
        self,
        *,
        read: Callable[..., dict[str, Any]],
        list_records: Callable[..., dict[str, Any]],
        upsert: Callable[..., dict[str, Any]],
        tombstone: Callable[..., dict[str, Any]],
        migration_epoch: str,
        policy_version: str,
    ) -> None:
        self._read = read
        self._list = list_records
        self._upsert = upsert
        self._tombstone = tombstone
        self.migration_epoch = str(migration_epoch or "").strip()
        self.policy_version = str(policy_version or "").strip()
        if not self.migration_epoch or not self.policy_version:
            raise ScopedProjectionError("scoped_projection_contract_invalid")

    def _context(
        self,
        *,
        kind: str,
        persona_id: str,
        identity_id: str = "",
        group_id: str = "",
        assurance: str = "verified",
    ) -> NamespaceContext:
        context = NamespaceContext(
            kind=kind, persona_id=persona_id, identity_id=identity_id, group_id=group_id,
            assurance=assurance, profile_status="active", policy_version=self.policy_version,
            migration_epoch=self.migration_epoch,
        )
        if context.errors():
            raise ScopedProjectionError(context.errors()[0])
        return context

    @staticmethod
    def _record(
        context: NamespaceContext,
        *,
        record_kind: str,
        record_id: str,
        domain: str,
        content: Any,
        approval_state: str = "not_applicable",
    ) -> ScopedProjectionRecord:
        return ScopedProjectionRecord(
            context=context,
            record_kind=record_kind,
            record_id=record_id,
            payload=build_scoped_domain_payload(
                domain=domain, source_kind=context.kind, content=_bounded(content),
                approval_state=approval_state,
            ),
        )

    def _learning_records(
        self, context: NamespaceContext, profile: Any, *, prefix: str
    ) -> list[ScopedProjectionRecord]:
        if not isinstance(profile, dict):
            return []
        result: list[ScopedProjectionRecord] = []
        approved = profile.get("learned_rules") if isinstance(profile.get("learned_rules"), list) else []
        pending = profile.get("pending_rules") if isinstance(profile.get("pending_rules"), list) else []
        evidence = {key: deepcopy(profile[key]) for key in _RULE_EVIDENCE_FIELDS if _present(profile.get(key))}
        if approved:
            result.append(self._record(
                context, record_kind="rule", record_id=f"{prefix}-rule-approved", domain="learning",
                content={"rules": approved}, approval_state="approved",
            ))
        if pending:
            result.append(self._record(
                context, record_kind="rule", record_id=f"{prefix}-rule-pending", domain="learning",
                content={"rules": pending}, approval_state="pending",
            ))
        if evidence:
            result.append(self._record(
                context, record_kind="evidence", record_id=f"{prefix}-rule-evidence", domain="learning",
                content=evidence, approval_state="pending",
            ))
        return result

    @staticmethod
    def _formal_people(snapshot: dict[str, Any]) -> list[str]:
        root = snapshot.get("unified_person") if isinstance(snapshot.get("unified_person"), dict) else {}
        profiles = root.get("profiles") if isinstance(root.get("profiles"), dict) else {}
        return sorted(
            str(person_id) for person_id, profile in profiles.items()
            if isinstance(profile, dict) and profile.get("profile_status", "active") == "active"
        )

    def build_records(
        self, snapshot: dict[str, Any], *, source_scope: str = "default"
    ) -> tuple[list[ScopedProjectionRecord], list[NamespaceContext]]:
        if not isinstance(snapshot, dict):
            raise ScopedProjectionError("scoped_projection_snapshot_invalid")
        persona_id = _persona_ref(source_scope)
        registry = UnifiedPersonRegistry(snapshot)
        people = self._formal_people(snapshot)
        records: list[ScopedProjectionRecord] = []
        contexts: dict[str, NamespaceContext] = {}

        def remember(context: NamespaceContext) -> None:
            contexts[context.cache_scope()] = context

        users = snapshot.get("users") if isinstance(snapshot.get("users"), dict) else {}
        by_person: dict[str, list[dict[str, Any]]] = {}
        for legacy_key, raw_user in users.items():
            if not isinstance(raw_user, dict):
                continue
            person_id = str(raw_user.get("unified_person_id") or "").strip()
            subject = str(raw_user.get("identity_subject_id") or raw_user.get("user_id") or legacy_key or "").strip()
            if person_id in people and subject and registry.matches_person_subject(person_id, subject):
                by_person.setdefault(person_id, []).append(raw_user)

        for person_id in people:
            matched = by_person.get(person_id, [])
            if len(matched) != 1:
                continue
            resolution = registry.formal_namespace_for_person(
                person_id, kind="private", policy_version=self.policy_version,
                migration_epoch=self.migration_epoch, purpose="profile_read",
            )
            raw_context = resolution.get("context") if isinstance(resolution, dict) else None
            if not resolution.get("ok") or not isinstance(raw_context, dict):
                continue
            context = self._context(
                kind="private", persona_id=persona_id, identity_id=person_id,
                assurance=str(raw_context.get("assurance") or "verified"),
            )
            remember(context)
            user = matched[0]
            profile_content = {
                key: deepcopy(user[key]) for key in ("nickname", "style", "profile_origin", "auto_profile_created")
                if _present(user.get(key))
            }
            if profile_content:
                records.append(self._record(
                    context, record_kind="profile_fact", record_id="req041-private-profile",
                    domain="profile", content=profile_content,
                ))
            for field in _PRIVATE_MEMORY_FIELDS:
                if _present(user.get(field)):
                    records.append(self._record(
                        context, record_kind="memory", record_id=f"req041-private-memory-{field.replace('_', '-')}",
                        domain="memory", content={field: deepcopy(user[field])},
                    ))
            records.extend(self._learning_records(context, user.get("expression_profile"), prefix="req041-private"))

        root = snapshot.get("unified_person") if isinstance(snapshot.get("unified_person"), dict) else {}
        links = root.get("identity_links") if isinstance(root.get("identity_links"), dict) else {}
        subject_people: dict[str, set[str]] = {}
        for link in links.values():
            if not isinstance(link, dict) or link.get("status") != "active":
                continue
            identity = link.get("identity") if isinstance(link.get("identity"), dict) else {}
            subject = str(identity.get("platform_subject_id") or "").strip()
            person_id = str(link.get("person_id") or "").strip()
            if subject and person_id in people and registry.matches_person_subject(person_id, subject):
                subject_people.setdefault(subject, set()).add(person_id)

        groups = snapshot.get("groups") if isinstance(snapshot.get("groups"), dict) else {}
        for legacy_group_key, raw_group in groups.items():
            if not isinstance(raw_group, dict):
                continue
            group_id = _group_ref(persona_id, raw_group.get("group_id") or legacy_group_key)
            if not group_id:
                continue
            shared = self._context(kind="group_shared", persona_id=persona_id, group_id=group_id)
            remember(shared)
            for field in _GROUP_MEMORY_FIELDS:
                if _present(raw_group.get(field)):
                    records.append(self._record(
                        shared, record_kind="memory", record_id=f"req041-group-memory-{field.replace('_', '-')}",
                        domain="memory", content={field: deepcopy(raw_group[field])},
                    ))
            records.extend(self._learning_records(shared, raw_group.get("expression_profile"), prefix="req041-group"))
            members = raw_group.get("members") if isinstance(raw_group.get("members"), dict) else {}
            for subject, member in members.items():
                candidates = subject_people.get(str(subject), set())
                if len(candidates) != 1 or not isinstance(member, dict):
                    continue
                person_id = next(iter(candidates))
                assurance = "verified"
                resolution = registry.formal_namespace_for_person(
                    person_id, kind="group_member", group_id=group_id,
                    policy_version=self.policy_version, migration_epoch=self.migration_epoch,
                    purpose="profile_read",
                )
                raw_context = resolution.get("context") if isinstance(resolution, dict) else None
                if not resolution.get("ok") or not isinstance(raw_context, dict):
                    continue
                assurance = str(raw_context.get("assurance") or assurance)
                member_context = self._context(
                    kind="group_member", persona_id=persona_id, identity_id=person_id,
                    group_id=group_id, assurance=assurance,
                )
                remember(member_context)
                member_profile = {
                    key: deepcopy(member[key]) for key in _MEMBER_PROFILE_FIELDS if _present(member.get(key))
                }
                if member_profile:
                    records.append(self._record(
                        member_context, record_kind="profile_fact", record_id="req041-group-member-profile",
                        domain="profile", content=member_profile,
                    ))
                member_memory = {
                    key: deepcopy(member[key]) for key in ("recent_phrases", "display_name_events")
                    if _present(member.get(key))
                }
                if member_memory:
                    records.append(self._record(
                        member_context, record_kind="memory", record_id="req041-group-member-observation",
                        domain="memory", content=member_memory,
                    ))
        return records, list(contexts.values())

    def sync_snapshot(self, snapshot: dict[str, Any], *, source_scope: str = "default") -> dict[str, Any]:
        records, contexts = self.build_records(snapshot, source_scope=source_scope)
        desired: dict[tuple[str, str], set[str]] = {}
        counts = {"created": 0, "updated": 0, "unchanged": 0, "cleared": 0, "tombstoned": 0, "errors": 0}
        errors: list[str] = []
        for record in records:
            scope = record.context.cache_scope()
            desired.setdefault((scope, record.record_kind), set()).add(record.record_id)
            current = self._read(record.context, record_kind=record.record_kind, record_id=record.record_id)
            if not isinstance(current, dict) or current.get("ok") is not True:
                counts["errors"] += 1
                errors.append(str((current or {}).get("code") or "scoped_read_failed")[:80])
                continue
            existing = current.get("record") if current.get("code") == "found" else None
            if isinstance(existing, dict) and _canonical(existing.get("payload")) == _canonical(record.payload):
                counts["unchanged"] += 1
                continue
            revision = int(existing.get("revision") or 0) + 1 if isinstance(existing, dict) else 1
            event_id = "req041-sync-" + hashlib.sha256(
                f"{scope}:{record.record_kind}:{record.record_id}:{revision}:{_hash(record.payload)}".encode("utf-8")
            ).hexdigest()[:48]
            result = self._upsert(
                record.context, record_kind=record.record_kind, record_id=record.record_id,
                revision=revision, payload=record.payload, event_id=event_id,
            )
            if not isinstance(result, dict) or result.get("ok") is not True:
                counts["errors"] += 1
                errors.append(str((result or {}).get("code") or "scoped_upsert_failed")[:80])
                continue
            counts["updated" if isinstance(existing, dict) else "created"] += 1

        context_map = {context.cache_scope(): context for context in contexts}
        for scope, context in context_map.items():
            for record_kind in ("profile_fact", "memory", "rule", "evidence"):
                listed = self._list(context, record_kind=record_kind, limit=1000)
                if not isinstance(listed, dict) or listed.get("ok") is not True:
                    counts["errors"] += 1
                    errors.append(str((listed or {}).get("code") or "scoped_list_failed")[:80])
                    continue
                keep = desired.get((scope, record_kind), set())
                for existing in listed.get("records") if isinstance(listed.get("records"), list) else []:
                    record_id = str(existing.get("record_id") or "") if isinstance(existing, dict) else ""
                    if not record_id.startswith(_RECORD_PREFIX) or record_id in keep:
                        continue
                    existing_payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
                    if existing_payload.get("content") in ({}, [], "", None):
                        continue
                    domain = str(existing_payload.get("domain") or "")
                    source_kind = str(existing_payload.get("source_kind") or context.kind)
                    approval_state = str(existing_payload.get("approval_state") or "not_applicable")
                    approved_by = str(existing_payload.get("approved_by") or "")
                    try:
                        cleared_payload = build_scoped_domain_payload(
                            domain=domain, source_kind=source_kind, content={},
                            approval_state=approval_state, approved_by=approved_by,
                        )
                    except Exception:
                        counts["errors"] += 1
                        errors.append("scoped_clear_payload_invalid")
                        continue
                    revision = int(existing.get("revision") or 0) + 1
                    event_id = "req041-clear-" + hashlib.sha256(
                        f"{scope}:{record_kind}:{record_id}:{revision}".encode("utf-8")
                    ).hexdigest()[:48]
                    result = self._upsert(
                        context, record_kind=record_kind, record_id=record_id,
                        revision=revision, payload=cleared_payload, event_id=event_id,
                    )
                    if isinstance(result, dict) and result.get("ok") is True:
                        counts["cleared"] += 1
                    else:
                        counts["errors"] += 1
                        errors.append(str((result or {}).get("code") or "scoped_clear_failed")[:80])
        return {
            "ok": counts["errors"] == 0,
            "code": "scoped_projection_synced" if counts["errors"] == 0 else "scoped_projection_degraded",
            "source_scope": source_scope,
            "records": len(records),
            **counts,
            "error_codes": sorted(set(errors))[:16],
        }


__all__ = [
    "ScopedProjectionError", "ScopedProjectionRecord", "ScopedProjectionSynchronizer",
]

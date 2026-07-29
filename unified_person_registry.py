"""Companion-owned Unified Person registry for the chat-side plugin.

This module is deliberately a small boundary around ``person_context_contract``:
the companion may create and link identities, while consumers only read the
contract projection.  Group overlays are scoped records and never become
profile facts.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any

try:
    from .person_context_contract import (
        build_identity_key,
        build_person_projection,
        ensure_person_store,
        person_id_for_identity,
        resolve_identity,
        validate_projection,
    )
except ImportError:
    from person_context_contract import (
        build_identity_key,
        build_person_projection,
        ensure_person_store,
        person_id_for_identity,
        resolve_identity,
        validate_projection,
    )


_LOCK = threading.RLock()
_FORBIDDEN = {
    "raw_prompt", "prompt", "private_object", "private_object_ref", "object",
    "chat_text", "content", "messages", "transcript", "database",
}
_IDENTITY_FIELDS = (
    "companion_instance_id", "bot_account_id", "adapter_instance_id",
    "subject_namespace", "platform_subject_id",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(value: Any, depth: int = 0) -> Any:
    """Copy only bounded JSON-like values and drop context-bearing fields."""
    if depth > 2 or value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:16]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            name = str(key).strip().lower()
            if not name or name in _FORBIDDEN:
                continue
            safe = _safe(item, depth + 1)
            if safe is not None:
                result[name[:80]] = safe
        return result
    return None


def _text(value: Any, field: str, limit: int = 200) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    value = value.strip()
    if not value or "\x00" in value or len(value) > limit:
        raise ValueError(f"{field}_invalid")
    return value


def _operation_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _text(value, "operation_id", 120)


def _identity(identity: Any) -> dict[str, str]:
    if not isinstance(identity, dict):
        raise ValueError("identity_invalid")
    # build_identity_key is the contract authority; this explicit check also
    # prevents accidental partial identity records from being persisted.
    normalized = {field: _text(identity.get(field), field) for field in _IDENTITY_FIELDS}
    normalized["subject_namespace"] = normalized["subject_namespace"].lower()
    build_identity_key(normalized)
    return normalized


def _root(store: dict[str, Any]) -> dict[str, Any]:
    ensure_person_store(store)
    root = store["unified_person"]
    if not isinstance(root.get("profiles"), dict):
        root["profiles"] = {}
    if not isinstance(root.get("identity_links"), dict):
        root["identity_links"] = {}
    if not isinstance(root.get("group_overlays"), dict):
        root["group_overlays"] = {}
    if not isinstance(root.get("audit_events"), list):
        root["audit_events"] = []
    if not isinstance(root.get("operations"), dict):
        root["operations"] = {}
    return root


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class UnifiedPersonRegistry:
    """The only chat-side writer for Unified Person identity state."""

    def __init__(self, store: dict[str, Any]) -> None:
        if not isinstance(store, dict):
            raise ValueError("store_invalid")
        self._store = store

    def status(self) -> dict[str, Any]:
        with _LOCK:
            try:
                root = _root(self._store)
            except (TypeError, ValueError):
                return {"state": "invalid", "profiles": 0, "identity_links": 0, "group_overlays": 0}
            profiles = root["profiles"]
            links = root["identity_links"]
            overlays = root["group_overlays"]
            state = "resolved" if profiles and links else "pending"
            if any(not isinstance(item, dict) for item in profiles.values()):
                state = "invalid"
            return {
                "state": state,
                "version": int(root.get("version") or 1),
                "profiles": len(profiles),
                "identity_links": len(links),
                "group_overlays": len(overlays),
            }

    def resolve(self, identity: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            try:
                result = resolve_identity(self._store, identity)
            except (TypeError, ValueError):
                return {"state": "invalid", "identity_key": "", "person_id": "", "errors": ["identity_invalid"]}
            if result.get("state") not in {"pending", "invalid", "resolved", "degraded"}:
                result["state"] = "invalid"
            return deepcopy(result)

    def create_or_link(
        self, identity: dict[str, Any], profile: dict[str, Any] | None = None,
        operation_id: str = "", actor_id: str = "companion", **_: Any,
    ) -> dict[str, Any]:
        """Create a person for an explicit operation, or return the existing link."""
        try:
            normalized = _identity(identity)
            op = _operation_id(operation_id)
            actor = _text(actor_id, "actor_id", 120)
        except ValueError:
            return {"ok": False, "state": "invalid", "code": "invalid_request", "person_id": ""}
        if not op:
            return {"ok": False, "state": "pending", "code": "explicit_operation_required", "person_id": "", "identity_key": build_identity_key(normalized)}
        safe_profile = _safe(profile or {})
        if not isinstance(safe_profile, dict):
            safe_profile = {}
        key = build_identity_key(normalized)
        person_id = person_id_for_identity(normalized)
        with _LOCK:
            root = _root(self._store)
            existing = root["identity_links"].get(key)
            if isinstance(existing, dict) and existing.get("person_id"):
                existing_id = str(existing["person_id"])
                projection = build_person_projection(self._store, existing_id)
                state = "resolved" if projection and not validate_projection(projection) else "invalid"
                return {"ok": state == "resolved", "state": state, "code": "already_linked", "person_id": existing_id, "identity_key": key, "projection": projection, "changed": False}
            now = _now()
            stored = {
                "person_id": person_id,
                "resolved_identity_key": key,
                "identity_keys": [key],
                "identity_assurance": "observed",
                "profile_status": "active",
                # The contract requires a non-empty display name.  Keep the
                # fallback generic; never derive it from message content.
                "display_name": str(safe_profile.get("display_name") or "unknown_person"),
                "aliases": safe_profile.get("aliases") if isinstance(safe_profile.get("aliases"), list) else [],
                "relation_policy_id": str(safe_profile.get("relation_policy_id") or "default_friend"),
                "owner_mode": str(safe_profile.get("owner_mode") or "not_owner"),
                "affinity_score": int(safe_profile.get("affinity_score") or 0),
                "group_overlay_ref": "",
                "projection_revision": 1,
                "updated_at": now,
            }
            root["profiles"][person_id] = stored
            root["identity_links"][key] = {
                "identity_key": key, "identity": normalized, "person_id": person_id,
                "identity_assurance": "observed", "status": "active",
                "created_at": now, "updated_at": now, "last_operation_id": op,
            }
            root["audit_events"].append({"event_id": op, "action": "create_or_link", "actor_id": actor, "person_id": person_id, "at": now})
            projection = build_person_projection(self._store, person_id)
            if projection is None or validate_projection(projection):
                return {"ok": False, "state": "invalid", "code": "projection_invalid", "person_id": person_id}
            return {"ok": True, "state": "resolved", "code": "created", "person_id": person_id, "identity_key": key, "projection": projection, "changed": True}

    def link_identity(self, person_id: str, identity: dict[str, Any], operation_id: str = "", actor_id: str = "companion", **_: Any) -> dict[str, Any]:
        try:
            person_id = _text(person_id, "person_id")
            normalized = _identity(identity)
            op = _operation_id(operation_id)
            _text(actor_id, "actor_id", 120)
        except ValueError:
            return {"ok": False, "state": "invalid", "code": "invalid_request", "person_id": ""}
        if not op:
            return {"ok": False, "state": "pending", "code": "explicit_operation_required", "person_id": person_id}
        key = build_identity_key(normalized)
        with _LOCK:
            root = _root(self._store)
            profile = root["profiles"].get(person_id)
            if not isinstance(profile, dict):
                return {"ok": False, "state": "pending", "code": "person_not_found", "person_id": person_id}
            prior = root["identity_links"].get(key)
            if isinstance(prior, dict) and prior.get("person_id") != person_id:
                return {"ok": False, "state": "invalid", "code": "identity_conflict", "person_id": person_id}
            root["identity_links"][key] = {"identity_key": key, "identity": normalized, "person_id": person_id, "identity_assurance": "explicit_linked", "status": "active", "updated_at": _now(), "last_operation_id": op}
            if key not in profile.setdefault("identity_keys", []):
                profile["identity_keys"].append(key)
            profile["identity_assurance"] = "explicit_linked"
            profile["projection_revision"] = int(profile.get("projection_revision") or 1) + 1
            profile["updated_at"] = _now()
            projection = build_person_projection(self._store, person_id)
            return {"ok": bool(projection and not validate_projection(projection)), "state": "resolved" if projection and not validate_projection(projection) else "invalid", "code": "identity_linked", "person_id": person_id, "identity_key": key, "projection": projection, "changed": True}

    def read_projection(self, person_id: str) -> dict[str, Any] | None:
        with _LOCK:
            projection = build_person_projection(self._store, str(person_id or ""))
            return deepcopy(projection) if projection and not validate_projection(projection) else None

    def upsert_group_overlay(self, person_id: str, group_scope: str, overlay: dict[str, Any], operation_id: str = "", actor_id: str = "companion", **_: Any) -> dict[str, Any]:
        try:
            person_id, group_scope, op = _text(person_id, "person_id"), _text(group_scope, "group_scope", 240), _operation_id(operation_id)
            _text(actor_id, "actor_id", 120)
        except ValueError:
            return {"ok": False, "state": "invalid", "code": "invalid_request", "person_id": person_id if isinstance(person_id, str) else ""}
        if not op:
            return {"ok": False, "state": "pending", "code": "explicit_operation_required", "person_id": person_id, "group_scope": group_scope}
        safe = _safe(overlay)
        if not isinstance(safe, dict):
            return {"ok": False, "state": "invalid", "code": "overlay_invalid", "person_id": person_id, "group_scope": group_scope}
        with _LOCK:
            root = _root(self._store)
            if not isinstance(root["profiles"].get(person_id), dict):
                return {"ok": False, "state": "pending", "code": "person_not_found", "person_id": person_id, "group_scope": group_scope}
            key = f"{person_id}:{_fingerprint(group_scope)[:32]}"
            previous = root["group_overlays"].get(key)
            revision = int(previous.get("revision") or 0) + 1 if isinstance(previous, dict) else 1
            root["group_overlays"][key] = {"person_id": person_id, "group_scope": group_scope, "overlay": safe, "revision": revision, "updated_at": _now(), "operation_id": op}
            return {"ok": True, "state": "resolved", "code": "group_overlay_upserted", "person_id": person_id, "group_scope": group_scope, "revision": revision, "changed": previous != root["group_overlays"][key]}

    def read_group_overlay(self, person_id: str, group_scope: str) -> dict[str, Any] | None:
        try:
            person_id, group_scope = _text(person_id, "person_id"), _text(group_scope, "group_scope", 240)
        except ValueError:
            return None
        with _LOCK:
            root = _root(self._store)
            key = f"{person_id}:{_fingerprint(group_scope)[:32]}"
            record = root["group_overlays"].get(key)
            if not isinstance(record, dict) or record.get("person_id") != person_id or record.get("group_scope") != group_scope:
                return None
            return deepcopy(record)


__all__ = ["UnifiedPersonRegistry"]

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
import re
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
    from .p4_affinity_confinement import validate_runtime_state
except ImportError:
    from person_context_contract import (
        build_identity_key,
        build_person_projection,
        ensure_person_store,
        person_id_for_identity,
        resolve_identity,
        validate_projection,
    )
    from p4_affinity_confinement import validate_runtime_state


_LOCK = threading.RLock()
_FORBIDDEN = {
    "raw_prompt", "prompt", "private_object", "private_object_ref", "object",
    "chat_text", "content", "messages", "transcript", "database",
}
_IDENTITY_FIELDS = (
    "companion_instance_id", "bot_account_id", "adapter_instance_id",
    "subject_namespace", "platform_subject_id",
)
_P4_EFFECT_VERSION = 1
_P4_EFFECT_ALLOWED_FIELDS = frozenset({
    "event_id", "occurred_at", "kind", "source_kind", "target_kind", "authority",
    "reason_code", "safe_reference", "safe_hash", "status", "shadow_only",
})
_P4_EFFECT_FORBIDDEN_FIELDS = frozenset({
    "raw_prompt", "prompt", "text", "content", "chat_text", "messages", "transcript",
    "private_object", "private_object_ref", "database", "db", "score", "penalty",
    "confinement_state", "confinement_until", "authorized", "owner",
})
_P4_EFFECT_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_P4_EFFECT_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
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


def _safe_affinity_score(value: Any) -> int:
    """Normalize optional profile affinity without letting malformed input abort creation."""
    if isinstance(value, bool):
        return 0
    try:
        score = int(float(value)) if value not in (None, "") else 0
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(-1200, min(1200, score))


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


def _contains_forbidden_key(value: Any) -> bool:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or key.strip().lower() in _P4_EFFECT_FORBIDDEN_FIELDS:
                return True
            if _contains_forbidden_key(item):
                return True
        return False
    if type(value) in (list, tuple):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _p4_effect_container(root: dict[str, Any]) -> dict[str, Any] | None:
    """Return the separate preparation ledger, without repairing corruption."""
    existing = root.get("p4_effect")
    if existing is None:
        existing = {"version": _P4_EFFECT_VERSION, "people": {}, "operations": {}}
        root["p4_effect"] = existing
    if not isinstance(existing, dict):
        return None
    if existing.get("version") != _P4_EFFECT_VERSION:
        return None
    if not isinstance(existing.get("people"), dict) or not isinstance(existing.get("operations"), dict):
        return None
    return existing


def _normalize_p4_effect_event(event: Any) -> tuple[dict[str, Any] | None, str]:
    if type(event) is not dict or _contains_forbidden_key(event):
        return None, "invalid_p4_effect_event"
    if any(type(key) is not str or key not in _P4_EFFECT_ALLOWED_FIELDS for key in event):
        return None, "invalid_p4_effect_event"
    if type(event.get("event_id")) is not str or _P4_EFFECT_TOKEN_RE.fullmatch(event["event_id"]) is None:
        return None, "invalid_p4_effect_event"
    occurred_at = event.get("occurred_at")
    if type(occurred_at) is not str or _P4_EFFECT_TIMESTAMP_RE.fullmatch(occurred_at) is None:
        return None, "invalid_p4_effect_event"
    try:
        parsed_occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_p4_effect_event"
    if parsed_occurred_at.tzinfo is None or parsed_occurred_at.utcoffset() is None:
        return None, "invalid_p4_effect_event"
    if type(event.get("kind")) is not str or _P4_EFFECT_TOKEN_RE.fullmatch(event["kind"]) is None:
        return None, "invalid_p4_effect_event"
    normalized: dict[str, Any] = {
        "event_id": event["event_id"],
        "occurred_at": occurred_at,
        "kind": event["kind"],
    }
    for field in ("source_kind", "target_kind", "authority", "reason_code", "safe_reference"):
        if field not in event:
            continue
        value = event[field]
        if type(value) is type(None):
            continue
        if type(value) is not str:
            return None, "invalid_p4_effect_event"
        if value == "":
            continue
        if _P4_EFFECT_TOKEN_RE.fullmatch(value) is None:
            return None, "invalid_p4_effect_event"
        normalized[field] = value
    if "safe_hash" in event:
        safe_hash = event["safe_hash"]
        if type(safe_hash) is not type(None):
            if type(safe_hash) is not str:
                return None, "invalid_p4_effect_event"
            if safe_hash and re.fullmatch(r"sha256:[0-9a-f]{64}", safe_hash) is None:
                return None, "invalid_p4_effect_event"
            if safe_hash:
                normalized["safe_hash"] = safe_hash
    if "status" in event:
        status = event.get("status")
        if type(status) is not str or status not in {"shadow", "invalid", "degraded"}:
            return None, "invalid_p4_effect_event"
        normalized["status"] = status
    if "shadow_only" in event:
        if event.get("shadow_only") is not True:
            return None, "invalid_p4_effect_event"
        normalized["shadow_only"] = True
    return normalized, ""


def _p4_effect_fingerprint(person_id: str, event: dict[str, Any]) -> str:
    return _fingerprint({"person_id": person_id, "event": event})


def _p4_effect_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    last = events[-1] if events else {}
    return {
        "mode": "effect_preparation",
        "event_count": len(events),
        "last_event_id": str(last.get("event_id") or ""),
        "last_kind": str(last.get("kind") or ""),
    }


def _p4_effect_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "effect_preparation",
        "event_count": max(0, min(512, int(state.get("event_count") or 0))),
        "last_kind": str(state.get("last_kind") or "")[:80],
    }


def _replay_p4_effect_entry(entry: Any, person_id: str) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]] | None, str]:
    if type(entry) is not dict or entry.get("person_id") != person_id:
        return None, None, "p4_effect_person_conflict"
    events = entry.get("events")
    if type(events) is not list or len(events) > 512:
        return None, None, "p4_effect_corrupt"
    normalized_events: list[dict[str, Any]] = []
    event_index: dict[str, dict[str, Any]] = {}
    for envelope in events:
        if type(envelope) is not dict or set(envelope) != {"event_id", "person_id", "origin_person_id", "event", "event_fingerprint", "recorded_at", "operation_id"}:
            return None, None, "p4_effect_corrupt"
        if envelope.get("person_id") != person_id or type(envelope.get("origin_person_id")) is not str or not envelope["origin_person_id"]:
            return None, None, "p4_effect_corrupt"
        event, error = _normalize_p4_effect_event(envelope.get("event"))
        if event is None or envelope.get("event_id") != event.get("event_id"):
            return None, None, error or "p4_effect_corrupt"
        event_id = event["event_id"]
        if event_id in event_index or envelope.get("event_fingerprint") != _p4_effect_fingerprint(envelope["origin_person_id"], event):
            return None, None, "p4_effect_corrupt"
        event_index[event_id] = deepcopy(envelope)
        normalized_events.append(event)
    state = _p4_effect_state(normalized_events)
    if entry.get("state") != state:
        return None, None, "p4_effect_state_mismatch"
    return state, event_index, ""


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
                "audit_events": len(root["audit_events"]),
                "operations": len(root["operations"]),
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
                "affinity_score": _safe_affinity_score(safe_profile.get("affinity_score")),
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

    def read_p4_effect_state(self, person_id: str) -> dict[str, Any]:
        """Read preparation state without creating a ledger or a person."""
        try:
            person_id = _text(person_id, "person_id")
        except ValueError:
            return {"ok": False, "code": "invalid_request", "person_id": ""}
        with _LOCK:
            root = _root(self._store)
            profile = root["profiles"].get(person_id)
            if not isinstance(profile, dict):
                return {"ok": False, "code": "person_not_found", "person_id": person_id}
            if profile.get("profile_status") != "active":
                return {"ok": False, "code": "person_not_active", "person_id": person_id}
            container = root.get("p4_effect")
            if container is None:
                state = _p4_effect_state([])
                return {
                    "ok": True,
                    "code": "p4_effect_empty",
                    "person_id": person_id,
                    "p4_effect_exists": False,
                    "p4_effect_state": state,
                    "p4_effect_summary": _p4_effect_summary(state),
                    "event_count": 0,
                }
            if _p4_effect_container(root) is None:
                return {"ok": False, "code": "p4_effect_corrupt", "person_id": person_id}
            entry = container["people"].get(person_id)
            if entry is None:
                state = _p4_effect_state([])
                return {
                    "ok": True,
                    "code": "p4_effect_empty",
                    "person_id": person_id,
                    "p4_effect_exists": False,
                    "p4_effect_state": state,
                    "p4_effect_summary": _p4_effect_summary(state),
                    "event_count": 0,
                }
            state, event_index, error = _replay_p4_effect_entry(entry, person_id)
            if error or state is None or event_index is None:
                return {"ok": False, "code": error or "p4_effect_corrupt", "person_id": person_id}
            return {
                "ok": True,
                "code": "p4_effect_read",
                "person_id": person_id,
                "p4_effect_exists": True,
                "p4_effect_state": state,
                "p4_effect_summary": _p4_effect_summary(state),
                "event_count": len(event_index),
            }

    def read_p4_live_state(self, person_id: str) -> dict[str, Any]:
        """Read a separately-owned live state without creating or repairing it."""
        try:
            person_id = _text(person_id, "person_id")
        except ValueError:
            return {"ok": False, "code": "invalid_request", "person_id": ""}
        with _LOCK:
            root = _root(self._store)
            profile = root["profiles"].get(person_id)
            if not isinstance(profile, dict) or profile.get("profile_status") != "active":
                return {"ok": False, "code": "person_not_active", "person_id": person_id}
            container = root.get("p4_live")
            if container is None:
                return {"ok": True, "code": "p4_live_state_absent", "person_id": person_id, "state": None}
            if type(container) is not dict or container.get("version") != 1 or type(container.get("people")) is not dict:
                return {"ok": False, "code": "p4_live_state_corrupt", "person_id": person_id}
            state = container["people"].get(person_id)
            if state is None:
                return {"ok": True, "code": "p4_live_state_absent", "person_id": person_id, "state": None}
            if type(state) is not dict:
                return {"ok": False, "code": "p4_live_state_corrupt", "person_id": person_id}
            return {"ok": True, "code": "p4_live_state_read", "person_id": person_id, "state": deepcopy(state)}

    def record_p4_live_state(
        self,
        person_id: str,
        state: dict[str, Any],
        *,
        operation_id: str,
        actor_id: str = "companion",
    ) -> dict[str, Any]:
        """Persist only an exact Companion-owned runtime state with replay safety."""
        try:
            person_id = _text(person_id, "person_id")
            operation_id = _text(operation_id, "operation_id", 120)
            actor_id = _text(actor_id, "actor_id", 120)
        except ValueError:
            return {"ok": False, "code": "invalid_request"}
        if actor_id != "companion" or validate_runtime_state(state) == "invalid":
            return {"ok": False, "code": "p4_live_state_rejected", "person_id": person_id, "operation_id": operation_id}
        copied_state = deepcopy(state)
        request_fingerprint = _fingerprint({"person_id": person_id, "state": copied_state, "actor_id": actor_id})
        with _LOCK:
            root = _root(self._store)
            profile = root["profiles"].get(person_id)
            if not isinstance(profile, dict) or profile.get("profile_status") != "active":
                return {"ok": False, "code": "person_not_active", "person_id": person_id, "operation_id": operation_id}
            container = root.get("p4_live")
            if container is None:
                container = {"version": 1, "people": {}, "operations": {}}
                root["p4_live"] = container
            if (
                type(container) is not dict
                or container.get("version") != 1
                or type(container.get("people")) is not dict
                or type(container.get("operations")) is not dict
            ):
                return {"ok": False, "code": "p4_live_state_corrupt", "person_id": person_id, "operation_id": operation_id}
            prior = container["operations"].get(operation_id)
            if isinstance(prior, dict):
                if prior.get("request_fingerprint") != request_fingerprint:
                    return {"ok": False, "code": "operation_id_conflict", "person_id": person_id, "operation_id": operation_id}
                result = deepcopy(prior.get("result"))
                if isinstance(result, dict):
                    result["idempotent"] = True
                    return result
                return {"ok": False, "code": "p4_live_state_corrupt", "person_id": person_id, "operation_id": operation_id}
            container["people"][person_id] = copied_state
            result = {"ok": True, "code": "p4_live_state_recorded", "person_id": person_id, "operation_id": operation_id, "changed": True}
            container["operations"][operation_id] = {"request_fingerprint": request_fingerprint, "result": deepcopy(result)}
            root["audit_events"].append({"event_id": operation_id, "action": "p4_live_state_recorded", "actor_id": actor_id, "person_id": person_id, "at": _now()})
            root["audit_events"] = root["audit_events"][-1000:]
            return result

    def record_p4_effect_event(
        self,
        person_id: str,
        event: dict[str, Any],
        *,
        operation_id: str,
        actor_id: str = "system",
    ) -> dict[str, Any]:
        """Append a replayable preparation event without enabling a live effect."""
        try:
            person_id = _text(person_id, "person_id")
            operation_id = _text(operation_id, "operation_id", 120)
            actor_id = _text(actor_id, "actor_id", 120)
        except ValueError:
            return {"ok": False, "code": "invalid_request"}
        normalized_event, error = _normalize_p4_effect_event(event)
        if normalized_event is None:
            return {"ok": False, "code": error or "invalid_p4_effect_event", "person_id": person_id, "operation_id": operation_id}
        payload_fingerprint = _fingerprint({"person_id": person_id, "event": normalized_event, "actor_id": actor_id})
        with _LOCK:
            root = _root(self._store)
            profile = root["profiles"].get(person_id)
            if not isinstance(profile, dict):
                return {"ok": False, "code": "person_not_found", "person_id": person_id, "operation_id": operation_id}
            if profile.get("profile_status") != "active":
                return {"ok": False, "code": "person_not_active", "person_id": person_id, "operation_id": operation_id}
            container = _p4_effect_container(root)
            if container is None:
                return {"ok": False, "code": "p4_effect_corrupt", "person_id": person_id, "operation_id": operation_id}
            previous_operation = container["operations"].get(operation_id)
            if isinstance(previous_operation, dict):
                if previous_operation.get("request_fingerprint") != payload_fingerprint:
                    return {"ok": False, "code": "operation_id_conflict", "person_id": person_id, "operation_id": operation_id}
                result = deepcopy(previous_operation.get("result") or {})
                if isinstance(result, dict):
                    result["idempotent"] = True
                    return result
                return {"ok": False, "code": "p4_effect_corrupt", "person_id": person_id, "operation_id": operation_id}

            people = container["people"]
            entry = people.get(person_id)
            if entry is None:
                now = _now()
                entry = {
                    "person_id": person_id,
                    "state": _p4_effect_state([]),
                    "events": [],
                    "created_at": now,
                    "updated_at": now,
                    "last_operation_id": "",
                }
            state, event_index, replay_error = _replay_p4_effect_entry(entry, person_id) if entry else (_p4_effect_state([]), {}, "")
            if replay_error or state is None or event_index is None:
                return {"ok": False, "code": replay_error or "p4_effect_corrupt", "person_id": person_id, "operation_id": operation_id}
            event_id = normalized_event["event_id"]
            fingerprint = _p4_effect_fingerprint(person_id, normalized_event)
            known = event_index.get(event_id)
            if known is not None:
                if known.get("event_fingerprint") != fingerprint:
                    return {"ok": False, "code": "p4_effect_event_id_conflict", "person_id": person_id, "event_id": event_id, "operation_id": operation_id}
                result = {
                    "ok": True,
                    "code": "p4_effect_event_duplicate",
                    "person_id": person_id,
                    "event_id": event_id,
                    "operation_id": operation_id,
                    "event_duplicate": True,
                    "p4_effect_summary": _p4_effect_summary(state),
                    "live_effect_permitted": False,
                }
                container["operations"][operation_id] = {"request_fingerprint": payload_fingerprint, "result": deepcopy(result)}
                return result
            now = _now()
            entry["events"].append({
                "event_id": event_id,
                "person_id": person_id,
                "origin_person_id": person_id,
                "event": deepcopy(normalized_event),
                "event_fingerprint": fingerprint,
                "recorded_at": now,
                "operation_id": operation_id,
            })
            replay_state = _p4_effect_state([item["event"] for item in entry["events"]])
            entry["state"] = replay_state
            entry["updated_at"] = now
            entry["last_operation_id"] = operation_id
            people[person_id] = entry
            root["audit_events"].append({
                "event_id": operation_id,
                "action": "p4_effect_event_recorded",
                "actor_id": actor_id,
                "person_id": person_id,
                "at": now,
                "kind": normalized_event["kind"],
            })
            root["audit_events"] = root["audit_events"][-1000:]
            result = {
                "ok": True,
                "code": "p4_effect_event_recorded",
                "person_id": person_id,
                "event_id": event_id,
                "operation_id": operation_id,
                "changed": True,
                "affected_person_ids": [person_id],
                "p4_effect_summary": _p4_effect_summary(replay_state),
                "live_effect_permitted": False,
            }
            container["operations"][operation_id] = {"request_fingerprint": payload_fingerprint, "result": deepcopy(result)}
            return result

    def guard_p4_effect_person_transition(
        self,
        action: str,
        source_person_id: str,
        target_person_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        """Reject unsupported identity merge/split before it can touch the P4 ledger.

        Chat-side Unified Person deliberately has no person-lifecycle merge or
        split operation.  A future caller must implement an explicit replay
        migration rather than silently reassigning preparation entries.
        """
        try:
            action = _text(action, "action", 40)
            source_person_id = _text(source_person_id, "source_person_id")
            target_person_id = _text(target_person_id, "target_person_id")
            operation_id = _text(operation_id, "operation_id", 120)
        except ValueError:
            return {"ok": False, "code": "invalid_request"}
        if action not in {"merge", "split"} or source_person_id == target_person_id:
            return {"ok": False, "code": "p4_effect_transition_rejected", "operation_id": operation_id}
        with _LOCK:
            # Do not call the P4 container helper here: creating or repairing
            # a ledger would itself violate the no-transition guarantee.
            root = _root(self._store)
            source = root["profiles"].get(source_person_id)
            target = root["profiles"].get(target_person_id)
            if not isinstance(source, dict) or not isinstance(target, dict):
                return {"ok": False, "code": "p4_effect_transition_person_not_found", "operation_id": operation_id}
            return {
                "ok": False,
                "code": "p4_effect_transition_unsupported",
                "operation_id": operation_id,
                "action": action,
            }

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

"""REQ-041 authoritative person-private memory with revision/CAS semantics."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time
from typing import Any


PRIVATE_MEMORY_FIELDS = (
    "companion_memory",
    "intent_profile",
    "dialogue_episodes",
    "open_loops",
    "behavior_habits",
    "action_preferences",
    "action_consequences",
    "state_continuity",
    "recent_reply_topics",
    "birthday_profile",
    "birthday_curiosity_opt_out",
    "birthday_curiosity_asked_at",
    "birthday_curiosity_answered_at",
    "episode_message_count",
    "last_episode_refresh_at",
    "dialogue_episode_retry_after",
    "dialogue_episode_last_error",
    "dialogue_episode_running_at",
    "last_memory_refresh_at",
    "companion_memory_retry_after",
    "companion_memory_last_error",
    "companion_memory_running_at",
)
_ROOT_KEY = "_req041_private_memory"
_SCHEMA = "req041.person_private_memory.v1"


class AuthoritativePrivateMemoryError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _token(value: Any, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        return ""
    return text


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise AuthoritativePrivateMemoryError("private_memory_depth_exceeded")
    if value is None or isinstance(value, (bool, int, str)):
        return value[:8000] if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuthoritativePrivateMemoryError("private_memory_number_invalid")
        return value
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1) for item in value[-512:]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:512]:
            if not isinstance(key, str) or not key or len(key) > 128:
                raise AuthoritativePrivateMemoryError("private_memory_key_invalid")
            result[key] = _bounded(item, depth=depth + 1)
        return result
    raise AuthoritativePrivateMemoryError("private_memory_value_invalid")


def private_memory_content(user: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(user, dict):
        raise AuthoritativePrivateMemoryError("private_memory_user_invalid")
    return {
        field: _bounded(deepcopy(user[field]))
        for field in PRIVATE_MEMORY_FIELDS
        if field in user and user[field] not in (None, "", [], {})
    }


def apply_private_memory_content(user: dict[str, Any], content: dict[str, Any]) -> None:
    if not isinstance(user, dict) or not isinstance(content, dict):
        raise AuthoritativePrivateMemoryError("private_memory_content_invalid")
    unexpected = set(content) - set(PRIVATE_MEMORY_FIELDS)
    if unexpected:
        raise AuthoritativePrivateMemoryError("private_memory_fields_invalid")
    for field in PRIVATE_MEMORY_FIELDS:
        if field in content:
            user[field] = _bounded(deepcopy(content[field]))
        else:
            user.pop(field, None)


class AuthoritativePrivateMemoryStore:
    def __init__(self, snapshot: dict[str, Any], *, clock: Any = None) -> None:
        if not isinstance(snapshot, dict):
            raise AuthoritativePrivateMemoryError("private_memory_snapshot_invalid")
        self.snapshot = snapshot
        self._clock = clock if callable(clock) else time.time

    def _root(self, *, create: bool = True) -> dict[str, Any] | None:
        root = self.snapshot.get(_ROOT_KEY)
        if root is None:
            if not create:
                return None
            root = {"schema": _SCHEMA, "records": {}}
            self.snapshot[_ROOT_KEY] = root
        if (
            not isinstance(root, dict)
            or root.get("schema") != _SCHEMA
            or not isinstance(root.get("records"), dict)
        ):
            raise AuthoritativePrivateMemoryError("private_memory_store_invalid")
        return root

    def read(self, person_id: str) -> dict[str, Any]:
        person = _token(person_id, 80)
        if not person:
            raise AuthoritativePrivateMemoryError("private_memory_person_invalid")
        root = self._root(create=False)
        if root is None:
            return {"ok": True, "code": "not_found", "record": None}
        raw = root["records"].get(person)
        if raw is None:
            return {"ok": True, "code": "not_found", "record": None}
        if not isinstance(raw, dict):
            raise AuthoritativePrivateMemoryError("private_memory_record_invalid")
        content = raw.get("content")
        revision = raw.get("revision")
        if (
            not isinstance(content, dict)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or raw.get("schema") != _SCHEMA
            or raw.get("content_hash") != _digest(content)
        ):
            raise AuthoritativePrivateMemoryError("private_memory_record_invalid")
        return {"ok": True, "code": "found", "record": deepcopy(raw)}

    def commit(
        self,
        person_id: str,
        content: dict[str, Any],
        *,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        person = _token(person_id, 80)
        operation = _token(operation_id, 160)
        if not person or not operation or isinstance(expected_revision, bool) or expected_revision < 0:
            raise AuthoritativePrivateMemoryError("private_memory_commit_invalid")
        if not isinstance(content, dict) or set(content) - set(PRIVATE_MEMORY_FIELDS):
            raise AuthoritativePrivateMemoryError("private_memory_fields_invalid")
        safe_content = {key: _bounded(deepcopy(value)) for key, value in content.items()}
        content_hash = _digest(safe_content)
        operation_hash = hashlib.sha256(operation.encode("utf-8")).hexdigest()
        request_hash = _digest({
            "person_id": person,
            "expected_revision": expected_revision,
            "content_hash": content_hash,
        })
        root = self._root()
        assert root is not None
        records = root["records"]
        current = records.get(person)
        current_revision = int(current.get("revision") or 0) if isinstance(current, dict) else 0
        if isinstance(current, dict) and current.get("last_operation_hash") == operation_hash:
            if current.get("last_request_hash") != request_hash:
                return {"ok": False, "code": "operation_id_conflict", "revision": current_revision}
            return {"ok": True, "code": "idempotent", "revision": current_revision, "record": deepcopy(current)}
        if current_revision != expected_revision:
            return {
                "ok": False,
                "code": "private_memory_revision_conflict",
                "revision": current_revision,
            }
        if isinstance(current, dict) and current.get("content_hash") == content_hash:
            return {
                "ok": True,
                "code": "unchanged",
                "revision": current_revision,
                "record": deepcopy(current),
            }
        record = {
            "schema": _SCHEMA,
            "revision": current_revision + 1,
            "content": safe_content,
            "content_hash": content_hash,
            "updated_at": float(self._clock()),
            "last_operation_hash": operation_hash,
            "last_request_hash": request_hash,
        }
        records[person] = record
        return {
            "ok": True,
            "code": "created" if current_revision == 0 else "updated",
            "revision": record["revision"],
            "record": deepcopy(record),
        }


__all__ = [
    "AuthoritativePrivateMemoryError",
    "AuthoritativePrivateMemoryStore",
    "PRIVATE_MEMORY_FIELDS",
    "apply_private_memory_content",
    "private_memory_content",
]

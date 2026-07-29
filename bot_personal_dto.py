# -*- coding: utf-8 -*-
"""Privacy-limited DTOs for the Bot Personal archive boundary."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any

from .bot_personal_contract import (
    BOT_PERSONAL_MEMORY_DOMAIN,
    BOT_PERSONAL_MEMORY_TYPES,
    BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
    BOT_PERSONAL_SUBJECT,
    TYPE_CONTRACTS,
    normalize_window,
    window_for_minutes,
)


FORBIDDEN_KEY_PARTS = {
    "prompt", "conversation", "chat_history", "transcript", "message_chain", "contexts",
    "cookie", "token", "password", "passwd", "secret", "credential", "authorization",
    "api_key", "apikey", "access_key", "private_key", "raw_message", "binary",
    "media_bytes", "media_binary", "media_data", "media_content", "media_blob",
    "image_bytes", "audio_bytes", "video_bytes", "base64",
}
CERTAINTY_ALIASES = {"high": 0.9, "medium": 0.6, "low": 0.3, "高": 0.9, "中": 0.6, "低": 0.3}


class PrivacyRejected(ValueError):
    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"privacy_rejected:{path}:{reason}")


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _certainty(value: Any, default: float = 0.6) -> float:
    if isinstance(value, str):
        value = CERTAINTY_ALIASES.get(value.strip().lower(), value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _parse_moment(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed


def _looks_sensitive_value(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    if re.search(r"(?:password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]", lowered):
        return "credential_pattern"
    if re.search(r"^bearer\s+", lowered):
        return "authorization_value"
    if "base64," in lowered or lowered.startswith("data:"):
        return "base64_or_data_uri"
    if "-----begin " in lowered:
        return "private_key_material"
    if re.search(r"(?:^|[\s=:;,])(?:[a-z]:[\\/]|\\\\|/home/|/root/|/tmp/|/var/|/volume\d+/)", text, re.IGNORECASE):
        return "absolute_path"
    return ""


def validate_bot_personal_key(value: Any, *, field: str = "idempotency_key") -> None:
    reason = _looks_sensitive_value(str(value or ""))
    if reason:
        raise PrivacyRejected(field, reason)
    if not _text(value, 240):
        raise PrivacyRejected(field, "missing")


def validate_bot_personal_payload(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    if depth > 8:
        raise PrivacyRejected(path, "max_depth")
    if isinstance(value, dict):
        if len(value) > 64:
            raise PrivacyRejected(path, "too_many_fields")
        for key, item in value.items():
            name = _text(key, 80)
            lowered = name.lower().replace("-", "_")
            if not name or any(part in lowered for part in FORBIDDEN_KEY_PARTS):
                raise PrivacyRejected(f"{path}.{name}", "forbidden_key")
            validate_bot_personal_payload(item, path=f"{path}.{name}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise PrivacyRejected(path, "too_many_items")
        for index, item in enumerate(value):
            validate_bot_personal_payload(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PrivacyRejected(path, "binary_media")
    if isinstance(value, str):
        reason = _looks_sensitive_value(value)
        if reason:
            raise PrivacyRejected(path, reason)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise PrivacyRejected(path, "unsupported_value")


def _safe_value(value: Any, *, depth: int = 0, max_depth: int = 8) -> Any:
    if depth > max_depth:
        return None
    if isinstance(value, dict):
        return {
            _text(key, 80): _safe_value(item, depth=depth + 1, max_depth=max_depth)
            for key, item in list(value.items())[:64]
            if _text(key, 80)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1, max_depth=max_depth) for item in list(value)[:64]]
    if isinstance(value, str):
        return _text(value, 1200)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value, 240)


def _derive_date(value: Any, occurred_at: str, now: datetime) -> str:
    explicit = _text(value, 32)
    if explicit:
        return explicit.split("T", 1)[0]
    parsed = _parse_moment(occurred_at) or now.astimezone()
    return parsed.date().isoformat()


def _derive_window(value: Any, occurred_at: str, now: datetime) -> str:
    explicit = normalize_window(value)
    parsed = _parse_moment(occurred_at) or now.astimezone()
    if explicit:
        return explicit
    return window_for_minutes(parsed.hour * 60 + parsed.minute)


@dataclass(frozen=True)
class BotPersonalArchiveDTO:
    record_id: str
    memory_domain: str
    memory_type: str
    subject: str
    date: str
    window: str
    window_date: str
    occurred_at: str
    created_at: str
    updated_at: str
    source_kind: str
    source_refs: list[str]
    certainty: float
    evidence_level: str
    status: str
    version: int
    idempotency_key: str
    payload_schema_version: str
    payload: dict[str, Any]

    def envelope(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "memory_domain": self.memory_domain,
            "memory_type": self.memory_type,
            "subject": self.subject,
            "date": self.date,
            "window": self.window,
            "window_date": self.window_date,
            "occurred_at": self.occurred_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_kind": self.source_kind,
            "source_refs": list(self.source_refs),
            "certainty": self.certainty,
            "evidence_level": self.evidence_level,
            "status": self.status,
            "version": self.version,
            "idempotency_key": self.idempotency_key,
            "payload_schema_version": self.payload_schema_version,
            "payload": deepcopy(self.payload),
        }


def build_bot_personal_dto(
    *,
    memory_type: str,
    kind: str = "",
    namespace: str = "",
    payload: dict[str, Any],
    idempotency_key: str,
    occurred_at: str,
    now: datetime | None = None,
    version: int = 1,
) -> BotPersonalArchiveDTO:
    del kind, namespace
    if memory_type not in BOT_PERSONAL_MEMORY_TYPES:
        raise ValueError(f"invalid_memory_type:{memory_type}")
    validate_bot_personal_payload(payload)
    validate_bot_personal_key(idempotency_key)
    current = now or datetime.now().astimezone()
    safe = _safe_value(payload) or {}
    occurred = _text(occurred_at, 80) or current.isoformat(timespec="seconds")
    date_key = _derive_date(safe.get("date") or safe.get("window_date"), occurred, current)
    window = _derive_window(safe.get("window"), occurred, current)
    if not window:
        raise ValueError("invalid_window")
    source_refs: list[str] = []
    for item in safe.get("source_refs") or []:
        ref = _text(item, 240)
        if ref and ref not in source_refs:
            source_refs.append(ref)
    if not source_refs:
        source_refs = [f"archive:{_text(idempotency_key, 240)}"]
    contract = TYPE_CONTRACTS[memory_type]
    source_kind, default_evidence, default_status = contract
    evidence = "L0" if memory_type == "bot_schedule_plan" else (_text(safe.get("evidence_level"), 8).upper() or default_evidence)
    if evidence not in {"L0", "L1", "L2", "L3"}:
        evidence = default_evidence
    created_at = _text(safe.get("created_at"), 80) or current.isoformat(timespec="seconds")
    updated_at = _text(safe.get("updated_at"), 80) or created_at
    record_id = _text(safe.get("record_id"), 160) or f"local:{_text(idempotency_key, 240)}"
    return BotPersonalArchiveDTO(
        record_id=record_id,
        memory_domain=BOT_PERSONAL_MEMORY_DOMAIN,
        memory_type=memory_type,
        subject=BOT_PERSONAL_SUBJECT,
        date=date_key,
        window=window,
        window_date=date_key,
        occurred_at=occurred,
        created_at=created_at,
        updated_at=updated_at,
        source_kind=source_kind,
        source_refs=source_refs,
        certainty=_certainty(safe.get("certainty"), 0.6),
        evidence_level=evidence,
        status=_text(safe.get("status"), 32) or default_status,
        version=max(1, int(version or 1)),
        idempotency_key=_text(idempotency_key, 240),
        payload_schema_version=BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
        payload=deepcopy(safe),
    )


def envelope_size_bytes(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

"""Read-only schema inspection for REQ-041 automatic migration sources.

The inspector deliberately records only structural metadata.  User identifiers,
profile values, chat text and arbitrary section names never enter its inventory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Sequence


INVENTORY_SCHEMA = "req041.source_inventory.v1"
SUPPORTED_STORE_VERSIONS = frozenset({1})
SUPPORTED_SECTION_SCHEMA_VERSIONS = frozenset({1})
_REQUIRED_STORE_SECTIONS = frozenset({"version", "users", "groups"})
_SQLITE_REQUIRED_COLUMNS = frozenset({
    "section_name", "payload_json", "updated_at", "checksum", "schema_version",
})
_SAFE_SECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class MigrationSourceInspectionError(ValueError):
    """Raised when a legacy source cannot be proven to match a supported store."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError as exc:
        raise MigrationSourceInspectionError("migration_source_unreadable") from exc


def _store_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in SUPPORTED_STORE_VERSIONS:
        raise MigrationSourceInspectionError("migration_source_store_version_unsupported")
    return value


def _validate_critical_sections(sections: dict[str, Any]) -> tuple[int, dict[str, bool]]:
    if not _REQUIRED_STORE_SECTIONS.issubset(sections):
        raise MigrationSourceInspectionError("migration_source_required_section_missing")
    version = _store_version(sections.get("version"))
    if not isinstance(sections.get("users"), dict) or not isinstance(sections.get("groups"), dict):
        raise MigrationSourceInspectionError("migration_source_section_shape_invalid")
    for optional_mapping in ("unified_person", "persona_lifecycle"):
        if optional_mapping in sections and not isinstance(sections[optional_mapping], dict):
            raise MigrationSourceInspectionError("migration_source_section_shape_invalid")
    return version, {
        "has_unified_person": isinstance(sections.get("unified_person"), dict),
        "has_persona_lifecycle": isinstance(sections.get("persona_lifecycle"), dict),
    }


def _inspect_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationSourceInspectionError("migration_source_json_invalid") from exc
    if not isinstance(payload, dict):
        raise MigrationSourceInspectionError("migration_source_root_invalid")
    if len(payload) > 512 or any(not isinstance(key, str) for key in payload):
        raise MigrationSourceInspectionError("migration_source_section_set_invalid")
    version, features = _validate_critical_sections(payload)
    return {
        "kind": "json",
        "store_version": version,
        "section_schema_versions": [],
        "section_count": len(payload),
        **features,
    }


def _inspect_sqlite(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15.0)
    except sqlite3.Error as exc:
        raise MigrationSourceInspectionError("migration_source_sqlite_invalid") from exc
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise MigrationSourceInspectionError("migration_source_sqlite_integrity_invalid")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_sections'"
        ).fetchone()
        if table is None:
            raise MigrationSourceInspectionError("migration_source_sqlite_contract_missing")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(store_sections)")}
        if not _SQLITE_REQUIRED_COLUMNS.issubset(columns):
            raise MigrationSourceInspectionError("migration_source_sqlite_contract_invalid")
        rows = connection.execute(
            "SELECT section_name,payload_json,schema_version FROM store_sections ORDER BY section_name"
        ).fetchall()
        if not rows or len(rows) > 512:
            raise MigrationSourceInspectionError("migration_source_section_set_invalid")
        sections: dict[str, Any] = {}
        schema_versions: set[int] = set()
        for raw_name, raw_payload, raw_schema_version in rows:
            name = str(raw_name or "")
            if _SAFE_SECTION_NAME.fullmatch(name) is None or name in sections:
                raise MigrationSourceInspectionError("migration_source_section_name_invalid")
            if (
                isinstance(raw_schema_version, bool)
                or not isinstance(raw_schema_version, int)
                or raw_schema_version not in SUPPORTED_SECTION_SCHEMA_VERSIONS
            ):
                raise MigrationSourceInspectionError("migration_source_section_version_unsupported")
            try:
                sections[name] = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise MigrationSourceInspectionError("migration_source_section_json_invalid") from exc
            schema_versions.add(raw_schema_version)
        version, features = _validate_critical_sections(sections)
        return {
            "kind": "sqlite",
            "store_version": version,
            "section_schema_versions": sorted(schema_versions),
            "section_count": len(sections),
            **features,
        }
    except sqlite3.Error as exc:
        raise MigrationSourceInspectionError("migration_source_sqlite_invalid") from exc
    finally:
        connection.close()


def inspect_migration_sources(
    data_dir: str | Path,
    source_files: Sequence[str | Path],
) -> dict[str, Any]:
    """Return a deterministic, content-free schema inventory for legacy stores."""
    root = Path(data_dir).resolve()
    inspected: list[dict[str, Any]] = []
    for source in source_files:
        candidate = Path(source)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            raise MigrationSourceInspectionError("migration_source_file_invalid")
        try:
            path = candidate.resolve(strict=True)
            path.relative_to(root)
        except (OSError, ValueError) as exc:
            raise MigrationSourceInspectionError("migration_source_path_invalid") from exc
        if not path.is_file():
            raise MigrationSourceInspectionError("migration_source_file_invalid")
        inspected.append(_inspect_sqlite(path) if _is_sqlite(path) else _inspect_json(path))
    if not inspected:
        raise MigrationSourceInspectionError("migration_source_missing")

    store_versions = sorted({int(item["store_version"]) for item in inspected})
    if len(store_versions) != 1:
        raise MigrationSourceInspectionError("migration_source_store_version_mixed")
    section_versions = sorted({
        int(version)
        for item in inspected
        for version in item["section_schema_versions"]
    })
    formats = {
        kind: sum(1 for item in inspected if item["kind"] == kind)
        for kind in ("json", "sqlite")
    }
    contract = {
        "store_version": store_versions[0],
        "section_schema_versions": section_versions,
        "formats": formats,
    }
    fingerprint = hashlib.sha256(_canonical(contract).encode("utf-8")).hexdigest()
    return {
        "schema": INVENTORY_SCHEMA,
        "source_schema_version": f"companion-v{store_versions[0]}-{fingerprint[:32]}",
        "fingerprint": fingerprint,
        "source_count": len(inspected),
        "formats": formats,
        "store_version": store_versions[0],
        "section_schema_versions": section_versions,
        "all_have_unified_person": all(item["has_unified_person"] for item in inspected),
        "all_have_persona_lifecycle": all(item["has_persona_lifecycle"] for item in inspected),
        "section_count_min": min(int(item["section_count"]) for item in inspected),
        "section_count_max": max(int(item["section_count"]) for item in inspected),
    }


__all__ = [
    "INVENTORY_SCHEMA", "MigrationSourceInspectionError", "inspect_migration_sources",
]

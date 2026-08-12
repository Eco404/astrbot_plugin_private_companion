from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from migration_source_inspector import (
    MigrationSourceInspectionError,
    inspect_migration_sources,
)


FIXTURE = Path(__file__).parent / "fixtures" / "req041" / "companion-v6.0.8-sanitized.json"


class MigrationSourceInspectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _copy_fixture(self, name: str = "companions.json") -> Path:
        target = self.data_dir / name
        shutil.copyfile(FIXTURE, target)
        return target

    def _sqlite_from_fixture(self) -> Path:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        target = self.data_dir / "companions.db"
        connection = sqlite3.connect(target)
        try:
            connection.execute(
                "CREATE TABLE store_sections ("
                "section_name TEXT PRIMARY KEY,payload_json TEXT NOT NULL,updated_at REAL NOT NULL,"
                "checksum TEXT DEFAULT '',schema_version INTEGER DEFAULT 1)"
            )
            connection.executemany(
                "INSERT INTO store_sections VALUES(?,?,0,'',1)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in payload.items()],
            )
            connection.commit()
        finally:
            connection.close()
        return target

    def test_detects_sanitized_v608_json_without_recording_user_values(self) -> None:
        source = self._copy_fixture()
        inventory = inspect_migration_sources(self.data_dir, [source])
        self.assertEqual("req041.source_inventory.v1", inventory["schema"])
        self.assertEqual(1, inventory["store_version"])
        self.assertEqual({"json": 1, "sqlite": 0}, inventory["formats"])
        self.assertTrue(inventory["all_have_unified_person"])
        encoded = json.dumps(inventory, ensure_ascii=False)
        for private_value in ("10001", "20001", "Fixture Owner", "fixture-private-sentinel"):
            self.assertNotIn(private_value, encoded)

    def test_json_and_sqlite_share_store_version_but_have_distinct_contract_fingerprints(self) -> None:
        json_source = self._copy_fixture()
        json_inventory = inspect_migration_sources(self.data_dir, [json_source])
        sqlite_source = self._sqlite_from_fixture()
        sqlite_inventory = inspect_migration_sources(self.data_dir, [sqlite_source])
        self.assertEqual(1, sqlite_inventory["store_version"])
        self.assertEqual([1], sqlite_inventory["section_schema_versions"])
        self.assertEqual({"json": 0, "sqlite": 1}, sqlite_inventory["formats"])
        self.assertNotEqual(json_inventory["source_schema_version"], sqlite_inventory["source_schema_version"])

    def test_rejects_invalid_json_root_missing_sections_and_unsupported_version(self) -> None:
        cases = (
            ("broken.json", "{", "migration_source_json_invalid"),
            ("list.json", "[]", "migration_source_root_invalid"),
            ("missing.json", '{"version":1,"users":{}}', "migration_source_required_section_missing"),
            ("future.json", '{"version":2,"users":{},"groups":{}}', "migration_source_store_version_unsupported"),
        )
        for name, content, error in cases:
            with self.subTest(name=name):
                path = self.data_dir / name
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(MigrationSourceInspectionError, error):
                    inspect_migration_sources(self.data_dir, [path])

    def test_rejects_sqlite_without_contract_bad_columns_or_invalid_section_json(self) -> None:
        missing = self.data_dir / "missing.db"
        connection = sqlite3.connect(missing)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(MigrationSourceInspectionError, "migration_source_sqlite_contract_missing"):
            inspect_migration_sources(self.data_dir, [missing])

        bad_columns = self.data_dir / "bad-columns.db"
        connection = sqlite3.connect(bad_columns)
        connection.execute("CREATE TABLE store_sections(section_name TEXT,payload_json TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(MigrationSourceInspectionError, "migration_source_sqlite_contract_invalid"):
            inspect_migration_sources(self.data_dir, [bad_columns])

        invalid_payload = self._sqlite_from_fixture()
        connection = sqlite3.connect(invalid_payload)
        connection.execute("UPDATE store_sections SET payload_json='{' WHERE section_name='users'")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(MigrationSourceInspectionError, "migration_source_section_json_invalid"):
            inspect_migration_sources(self.data_dir, [invalid_payload])

    def test_rejects_symlink_and_path_escape(self) -> None:
        outside = self.data_dir.parent / f"{self.data_dir.name}-outside.json"
        shutil.copyfile(FIXTURE, outside)
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.data_dir / "linked.json"
        link.symlink_to(outside)
        with self.assertRaisesRegex(MigrationSourceInspectionError, "migration_source_file_invalid"):
            inspect_migration_sources(self.data_dir, [link])
        with self.assertRaisesRegex(MigrationSourceInspectionError, "migration_source_path_invalid"):
            inspect_migration_sources(self.data_dir, [outside])


if __name__ == "__main__":
    unittest.main()

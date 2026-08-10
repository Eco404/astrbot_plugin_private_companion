from __future__ import annotations

import ast
import asyncio
import copy
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import types
import unittest
from typing import Any

from migration_coordinator import MigrationCoordinator
from migration_backfill import MigrationBackfill
from migration_outbox import MigrationOutbox
from unified_person_registry import UnifiedPersonRegistry


ROOT = Path(__file__).resolve().parents[1]


def _load_methods(*names: str) -> dict[str, Any]:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPlugin")
    selected = [copy.deepcopy(node) for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in selected:
        node.decorator_list = []
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": Any,
        "Path": Path,
        "asyncio": asyncio,
        "deepcopy": deepcopy,
        "hashlib": hashlib,
        "MigrationBackfill": MigrationBackfill,
        "_single_line": lambda value, limit=240: " ".join(str(value or "").split())[:limit],
        "logger": types.SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    }
    exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
    return {name: namespace[name] for name in names}


METHODS = _load_methods(
    "_req041_migration_source_files",
    "_req041_compatibility_snapshot",
    "_req041_initialize_automatic_migration",
)


class Harness:
    _req041_migration_source_files = METHODS["_req041_migration_source_files"]
    _req041_compatibility_snapshot = METHODS["_req041_compatibility_snapshot"]
    _req041_initialize_automatic_migration = METHODS["_req041_initialize_automatic_migration"]


class MigrationStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _host(self, *, source: bool = True, bind: bool = True) -> Harness:
        host = Harness()
        host.data_dir = str(self.data_dir)
        host.data_file = str(self.data_dir / "companions.json")
        host.storage_backend = "json"
        host.storage_sqlite_effective_path = str(self.data_dir / "companions.db")
        host._persona_profiles_dir = str(self.data_dir / "persona_profiles")
        if source:
            Path(host.data_file).write_text('{"users":{"u1":{}}}', encoding="utf-8")
        host._data_lock = asyncio.Lock()
        host.data = {"users": {}}
        host.plugin_identity = {"version": "6.1.1"}
        host.req041_migration_coordinator = MigrationCoordinator(self.data_dir)
        host.req041_migration_outbox = MigrationOutbox(self.data_dir / "req041_migration_outbox.db")
        host.req041_migration_status = {}
        host.enable_auto_user_profile_creation = True
        host.default_enable_configured_targets = False
        host.enable_proactive_only_mode = False
        host.proactive_intensity_preset = "off"
        host.enable_photo_text_action = True
        host.enable_screen_glance_action = False
        host.enable_poke_action = False
        host.enable_voice_action = True
        host.enable_relationship_content_tiers = True
        host.target_user_id = "redacted-by-boolean"
        host.enable_custom_relationship_stage_policy = True
        host.relationship_positive_stage_cap_key = "close"
        host._memory_companion_presence = lambda: {"version": "1.7.2"}
        host._memory_companion_bridge = lambda: object() if bind else None
        host.bind_calls = []

        def binder(_bridge, **kwargs):
            host.bind_calls.append(kwargs)
            return {"ok": True, "state": "ready", "code": "bound"}

        host._memory_companion_bind_namespace_epoch = binder
        return host

    async def test_new_install_without_source_requires_no_action(self) -> None:
        host = self._host(source=False)
        await host._req041_initialize_automatic_migration()
        self.assertEqual("not_required", host.req041_migration_status["state"])
        self.assertFalse(host.req041_migration_status["required"])
        self.assertEqual({}, host.req041_migration_coordinator.status())

    async def test_existing_install_auto_backs_up_captures_policy_starts_outbox_and_binds_memory(self) -> None:
        host = self._host()
        await host._req041_initialize_automatic_migration()
        status = host.req041_migration_coordinator.status()
        self.assertEqual("S4", status["phase"])
        self.assertTrue(host.req041_migration_coordinator.verify_backup())
        self.assertEqual("active", host.req041_migration_status["state"])
        self.assertTrue(host.req041_migration_status["memory_bound"])
        self.assertEqual(1, len(host.bind_calls))
        self.assertEqual(status["migration_epoch"], host.bind_calls[0]["migration_epoch"])
        self.assertEqual(status["policy_version"], host.bind_calls[0]["policy_version"])
        outbox = host.req041_migration_outbox.epoch_status(status["migration_epoch"])
        self.assertEqual("active", outbox["state"])
        compatibility = __import__("json").loads(status["compatibility_json"])
        self.assertNotIn("target_user_id", str(compatibility))
        self.assertTrue(compatibility["owner_policy"]["configured_target"])

    async def test_missing_memory_degrades_only_shadow_and_restart_reuses_epoch(self) -> None:
        host = self._host(bind=False)
        await host._req041_initialize_automatic_migration()
        first = host.req041_migration_coordinator.status()
        self.assertEqual("S4", first["phase"])
        self.assertEqual("degraded", host.req041_migration_status["state"])
        self.assertEqual("memory_bridge_unavailable", host.req041_migration_status["code"])
        await host._req041_initialize_automatic_migration()
        second = host.req041_migration_coordinator.status()
        self.assertEqual(first["migration_epoch"], second["migration_epoch"])
        self.assertEqual("S4", second["phase"])

    async def test_startup_backfills_only_explicitly_linked_legacy_user(self) -> None:
        host = self._host()
        registry = UnifiedPersonRegistry(host.data)
        identity = {
            "companion_instance_id": "astrbot_plugin_private_companion",
            "bot_account_id": "onebot:bot-1",
            "adapter_instance_id": "onebot:default",
            "subject_namespace": "onebot:user",
            "platform_subject_id": "10001",
        }
        person = registry.create_or_link(identity, operation_id="startup-fixture")
        host.data["users"] = {
            "10001": {
                "unified_person_id": person["person_id"],
                "relationship_role": "owner",
                "relationship_mode": "normal",
                "relationship_score": 88,
            }
        }
        await host._req041_initialize_automatic_migration()
        account = host.req041_relationship_store.account(
            __import__("identity_namespace").NamespaceContext(
                kind="private",
                identity_id=person["person_id"],
                group_id="",
                assurance="verified",
                profile_status="active",
                policy_version="req041-v1",
                migration_epoch=host.req041_migration_coordinator.status()["migration_epoch"],
            )
        )
        self.assertEqual("S4", host.req041_migration_status["phase"])
        self.assertEqual(1, host.req041_migration_status["s4"]["migrated"])
        self.assertEqual("owner", account["relationship_role"])
        self.assertEqual("normal", account["relationship_mode"])
        self.assertEqual(88, account["relationship_score"])

    async def test_external_sqlite_path_fails_safe_without_blocking_legacy_runtime(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        database = outside / "companions.db"
        database.write_bytes(b"not sqlite but explicit active store")
        host = self._host(source=False)
        host.storage_backend = "sqlite"
        host.storage_sqlite_effective_path = str(database)
        await host._req041_initialize_automatic_migration()
        self.assertEqual("degraded", host.req041_migration_status["state"])
        self.assertIn("migration_source_path_invalid", host.req041_migration_status["code"])

    def test_initialize_schedules_migration_before_scheduler_and_maintenance(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        migration = source.index('"req041_automatic_migration"')
        scheduler = source.index("self._task = asyncio.create_task(self._scheduler_loop())")
        maintenance = source.index("self._startup_maintenance_task = asyncio.create_task")
        self.assertLess(migration, scheduler)
        self.assertLess(migration, maintenance)


if __name__ == "__main__":
    unittest.main()

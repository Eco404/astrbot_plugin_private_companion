from __future__ import annotations

import ast
import copy
from pathlib import Path
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load_gate() -> Any:
    tree = ast.parse((ROOT / "user_memory.py").read_text(encoding="utf-8"))
    owner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserMemoryMixin"
    )
    method = next(
        copy.deepcopy(node) for node in owner.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_req041_private_memory_write_allowed"
    )
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(module, str(ROOT / "user_memory.py"), "exec"), namespace)
    return namespace["_req041_private_memory_write_allowed"]


class _Host:
    _req041_private_memory_write_allowed = _load_gate()


class ScopedMemoryWriteGateTests(unittest.TestCase):
    def test_unmanaged_legacy_install_remains_compatible(self) -> None:
        host = _Host()
        host.req041_scoped_projection_sync = None
        host.req041_migration_status = {"required": False}
        self.assertTrue(host._req041_private_memory_write_allowed({"user_id": "u1"}))

    def test_fresh_or_migrating_runtime_without_memory_bridge_fails_closed(self) -> None:
        for status in (
            {"required": True},
            {"required": False, "scoped_required": True},
        ):
            with self.subTest(status=status):
                host = _Host()
                host.req041_scoped_projection_sync = None
                host.req041_migration_status = status
                self.assertFalse(host._req041_private_memory_write_allowed({"user_id": "u1"}))

    def test_scoped_runtime_requires_a_formal_private_namespace(self) -> None:
        host = _Host()
        host.req041_scoped_projection_sync = object()
        host.req041_migration_status = {"scoped_required": True}
        calls: list[tuple[str, str]] = []

        def resolver(_user, *, kind, purpose):
            calls.append((kind, purpose))
            return object() if _user.get("formal") else None

        host._req041_scoped_context_for_user = resolver
        self.assertFalse(host._req041_private_memory_write_allowed({"formal": False}))
        self.assertTrue(host._req041_private_memory_write_allowed({"formal": True}))
        self.assertEqual([("private", "memory_write"), ("private", "memory_write")], calls)

    def test_missing_or_faulting_resolver_fails_closed(self) -> None:
        host = _Host()
        host.req041_scoped_projection_sync = object()
        host.req041_migration_status = {"scoped_required": True}
        self.assertFalse(host._req041_private_memory_write_allowed({"formal": True}))
        host._req041_scoped_context_for_user = lambda *_args, **_kwargs: 1 / 0
        self.assertFalse(host._req041_private_memory_write_allowed({"formal": True}))

    def test_private_pipeline_guards_all_durable_memory_mutations(self) -> None:
        source = (ROOT / "message_pipeline.py").read_text(encoding="utf-8")
        gate = source.index("private_memory_write_allowed = self._req041_private_memory_write_allowed(user)")
        guarded = source.index("if private_memory_write_allowed:", gate)
        for call in (
            "self._update_companion_memory_from_message",
            "self._update_open_loops_from_message",
            "self._update_action_preferences_from_message",
            "self._update_user_behavior_habits_from_message",
        ):
            self.assertGreater(source.index(call, guarded), guarded)


if __name__ == "__main__":
    unittest.main()

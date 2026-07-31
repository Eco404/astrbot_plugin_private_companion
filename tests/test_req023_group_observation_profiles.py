from __future__ import annotations

import ast
import copy
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _single_line(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _load_methods(filename: str, class_name: str, names: set[str]) -> dict[str, Any]:
    source = (ROOT / filename).read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    namespace: dict[str, Any] = {
        "Any": Any,
        "AstrMessageEvent": Any,
        "re": re,
        "_single_line": _single_line,
        "_now_ts": lambda: 123.0,
    }
    for node in owner.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            module = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, str(ROOT / filename), "exec"), namespace)
    return {name: namespace[name] for name in names}


WORLD_BOOK_METHODS = _load_methods(
    "worldbook.py",
    "WorldbookMixin",
    {
        "_ensure_worldbook_group_observation_profile",
        "_confirm_worldbook_observation_profile_name",
        "_worldbook_profile_by_user_id",
    },
)
EVENT_METHODS = _load_methods("event_dispatch.py", "EventDispatchMixin", {"_sender_qq_nickname"})


class _WorldbookHost:
    enable_worldbook_member_recognition = True

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


for _name, _method in WORLD_BOOK_METHODS.items():
    setattr(_WorldbookHost, _name, _method)


class GroupObservationProfileTests(unittest.TestCase):
    def test_qq_nickname_precedes_group_card(self) -> None:
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                sender=SimpleNamespace(nickname="QQ昵称", card="很抽象的群名片"),
                raw_message={"sender": {"nickname": "另一个昵称", "card": "群名片"}},
            ),
            raw_message={},
            get_sender_nickname=lambda: "回退昵称",
        )
        self.assertEqual("QQ昵称", EVENT_METHODS["_sender_qq_nickname"](object(), event))

    def test_observed_profile_is_neutral_and_not_a_private_or_proactive_target(self) -> None:
        host = _WorldbookHost()
        profile = host._ensure_worldbook_group_observation_profile(
            group_id="group-a", sender_id="10001", qq_nickname="QQ昵称", group_card="群名片"
        )

        self.assertIsNotNone(profile)
        self.assertEqual("QQ昵称", profile["name"])
        self.assertEqual(["群名片"], profile["group_aliases"]["group-a"])
        self.assertTrue(profile["observation_only"])
        self.assertFalse(profile["proactive_contact_enabled"])
        self.assertFalse(profile["private_memory_enabled"])
        self.assertFalse(profile["cross_group_memory_enabled"])
        self.assertFalse(profile["p4_eligible"])
        self.assertEqual(0, profile["affinity_score"])
        self.assertEqual("neutral", profile["relationship_state"])
        self.assertNotIn("users", host.data)
        self.assertIsNone(host._worldbook_profile_by_user_id("10001"))
        self.assertEqual("QQ昵称", host._worldbook_profile_by_user_id("10001", include_observation=True)["name"])

    def test_repeat_and_cross_group_observation_are_idempotent_and_group_scoped(self) -> None:
        host = _WorldbookHost()
        first = host._ensure_worldbook_group_observation_profile(
            group_id="group-a", sender_id="10001", qq_nickname="QQ昵称", group_card="甲群名片"
        )
        again = host._ensure_worldbook_group_observation_profile(
            group_id="group-a", sender_id="10001", qq_nickname="QQ昵称", group_card="甲群名片"
        )
        second_group = host._ensure_worldbook_group_observation_profile(
            group_id="group-b", sender_id="10001", qq_nickname="QQ昵称", group_card="乙群名片"
        )

        self.assertIs(first, again)
        self.assertIs(first, second_group)
        self.assertEqual(["10001"], list(host.data["worldbook_member_profiles"]))
        self.assertEqual(["甲群名片"], first["group_aliases"]["group-a"])
        self.assertEqual(["乙群名片"], first["group_aliases"]["group-b"])
        self.assertEqual({"group-a", "group-b"}, set(first["group_observation_scope_ids"]))

    def test_explicit_name_confirmation_preserves_neutral_observation_boundaries(self) -> None:
        host = _WorldbookHost()
        profile = host._ensure_worldbook_group_observation_profile(
            group_id="group-a", sender_id="10001", qq_nickname="QQ昵称", group_card="群名片"
        )
        before = {key: profile[key] for key in ("affinity_score", "relationship_state", "group_aliases")}

        self.assertTrue(
            host._confirm_worldbook_observation_profile_name(
                profile, sender_id="10001", name="小雪", aliases=["雪雪", "QQ昵称"]
            )
        )
        self.assertEqual("小雪", profile["name"])
        self.assertIn("QQ昵称", profile["aliases"])
        self.assertEqual(before["affinity_score"], profile["affinity_score"])
        self.assertEqual(before["relationship_state"], profile["relationship_state"])
        self.assertEqual(before["group_aliases"], profile["group_aliases"])
        self.assertFalse(profile["proactive_contact_enabled"])
        self.assertFalse(profile["p4_eligible"])

    def test_group_path_and_panel_keep_observation_profiles_local_only(self) -> None:
        group_source = (ROOT / "group_observation.py").read_text(encoding="utf-8")
        worldbook_source = (ROOT / "worldbook.py").read_text(encoding="utf-8")
        api_source = (ROOT / "page_api.py").read_text(encoding="utf-8")
        panel_source = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")

        self.assertIn("_ensure_worldbook_group_observation_profile", group_source)
        self.assertIn("include_observation=True", group_source)
        self.assertIn("profile.get(\"observation_only\")", worldbook_source)
        self.assertIn('"proactive_contact_enabled": bool(item.get("proactive_contact_enabled", False))', api_source)
        self.assertIn("仅观察", panel_source)
        self.assertNotIn("self._get_user(sender_id)", group_source)


if __name__ == "__main__":
    unittest.main()

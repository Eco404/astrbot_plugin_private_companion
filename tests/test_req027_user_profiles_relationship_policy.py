from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _single_line(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _load_relationship_policy():
    spec = importlib.util.spec_from_file_location("req027_relationship_policy", ROOT / "relationship_policy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_platform_compat():
    package_name = "req027_platform_package"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    helpers = ModuleType(f"{package_name}.helpers")
    helpers._single_line = _single_line
    sys.modules[package_name] = package
    sys.modules[helpers.__name__] = helpers
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.platform_compat",
        ROOT / "platform_compat.py",
        submodule_search_locations=[],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_class_methods(filename: str, class_name: str, names: set[str], namespace: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    scope = dict(namespace)
    for node in owner.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            module = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, str(path), "exec"), scope)
    return {name: scope[name] for name in names}


CORE_METHODS = _load_class_methods(
    "core_store.py",
    "CoreStoreMixin",
    {
        "_auto_profile_platform_set",
        "_auto_profile_nickname",
        "_ensure_auto_private_user_profile",
        "_is_target_private_user",
    },
    {
        "Any": Any,
        "re": re,
        "_single_line": _single_line,
        "_safe_int": lambda value, default=0, minimum=None, maximum=None: max(
            minimum if minimum is not None else int(float(value)),
            min(maximum if maximum is not None else int(float(value)), int(float(value))),
        ) if str(value).strip() else default,
        "_safe_float": lambda value, default=0.0: float(value or default),
        "_now_ts": lambda: 123.0,
    },
)


class _AutoProfileHost:
    enable_auto_user_profile_creation = False
    auto_enable_companion_for_new_users = False
    auto_profile_platforms = ["onebot", "qq_official", "telegram", "webchat", "generic"]
    default_nickname_strategy = "platform_display_name"
    default_nickname = "你"
    default_style = "温柔"
    default_relationship_score = 700
    default_proactive_enabled = False
    default_proactive_daily_limit = 0

    def __init__(self) -> None:
        self.data = {"users": {}}
        self.saved = 0
        self.platform_kind = "telegram"
        self.bot_ids: set[str] = set()
        self.targets: list[str] = []

    def _canonical_private_user_id(self, value: str) -> str:
        return str(value or "").strip()

    def _is_bot_self_user_id(self, value: str) -> bool:
        return value in self.bot_ids

    def _platform_kind_for_event(self, _event: Any) -> str:
        return self.platform_kind

    def _normalize_platform_kind(self, value: Any) -> str:
        return _load_platform_compat().PlatformCompatibilityMixin._normalize_platform_kind(value)

    def _configured_target_ids(self) -> list[str]:
        return list(self.targets)

    def _get_user(self, user_id: str) -> dict[str, Any]:
        return self.data["users"].setdefault(
            user_id,
            {"enabled": False, "relationship_role": "friend", "relationship_score": 7},
        )

    def _note_private_user_umo(self, _user_id: str, user: dict[str, Any], umo: str) -> None:
        user["umo"] = umo

    def _schedule_data_save(self) -> None:
        self.saved += 1


for _name, _method in CORE_METHODS.items():
    setattr(_AutoProfileHost, _name, _method)


class Req027UserProfileRelationshipPolicyTests(unittest.TestCase):
    def test_telegram_aliases_and_profile_are_first_class(self) -> None:
        module = _load_platform_compat()
        mixin = module.PlatformCompatibilityMixin
        for alias in ("telegram", "telegram_bot", "telegrambot", "tg"):
            with self.subTest(alias=alias):
                self.assertEqual("telegram", mixin._normalize_platform_kind(alias))
        profile = mixin()._platform_profile(kind="telegram")
        self.assertEqual("Telegram", profile["label"])
        self.assertEqual("Telegram 用户ID", profile["identity_label"])
        self.assertTrue(profile["capabilities"]["private_proactive"])
        self.assertFalse(profile["capabilities"]["onebot_actions"])

    def test_relationship_policy_boundaries_and_security_whitelist(self) -> None:
        policy = _load_relationship_policy()
        boundaries = [
            (-1200, "deeply_distant"), (-801, "deeply_distant"),
            (-800, "strongly_distant"), (-401, "strongly_distant"),
            (-400, "distant"), (-1, "distant"),
            (0, "acquaintance"), (199, "acquaintance"),
            (200, "familiar"), (599, "familiar"),
            (600, "close"), (899, "close"),
            (900, "intimate"), (1199, "intimate"),
            (1200, "deeply_bonded"),
        ]
        for score, key in boundaries:
            with self.subTest(score=score):
                self.assertEqual(key, policy.relationship_stage_for_score(score)["phase"]["key"])

        hostile = [{
            "key": "acquaintance",
            "min": -999999,
            "max": 999999,
            "label": "自定义初识",
            "tone": "自然",
            "owner": True,
            "cross_user_query": True,
            "p4_bypass": True,
            "allow_followup": "false",
        }, {"key": "forged_stage", "label": "伪造阶段"}]
        normalized = policy.normalize_relationship_stage_policy(hostile)
        self.assertEqual(8, len(normalized))
        acquaintance = next(item for item in normalized if item["key"] == "acquaintance")
        self.assertEqual((0, 199), (acquaintance["min"], acquaintance["max"]))
        self.assertEqual("自定义初识", acquaintance["label"])
        self.assertTrue(acquaintance["allow_followup"])
        for forbidden in ("owner", "cross_user_query", "p4_bypass"):
            self.assertNotIn(forbidden, acquaintance)
        self.assertNotIn("forged_stage", {item["key"] for item in normalized})

    def test_auto_profile_is_minimal_idempotent_and_not_authoritative(self) -> None:
        host = _AutoProfileHost()
        host.enable_auto_user_profile_creation = True
        event = SimpleNamespace(unified_msg_origin="telegram:FriendMessage:10001")
        user, created = host._ensure_auto_private_user_profile(
            event, user_id="10001", sender_display_name="小雪", now=456.0
        )
        self.assertTrue(created)
        self.assertEqual("小雪", user["nickname"])
        self.assertEqual("friend", user["relationship_role"])
        self.assertEqual(7, user["relationship_score"])
        self.assertFalse(user["enabled"])
        self.assertFalse(user["auto_enabled"])
        self.assertFalse(user["manual_enabled"])
        self.assertFalse(user["manual_disabled"])
        self.assertEqual(0, user["proactive_daily_limit"])
        self.assertEqual("private_auto", user["profile_origin"])
        self.assertNotIn("owner", user)
        self.assertEqual(1, host.saved)

        same, duplicate_created = host._ensure_auto_private_user_profile(
            event, user_id="10001", sender_display_name="另一个名字", now=999.0
        )
        self.assertIs(user, same)
        self.assertFalse(duplicate_created)
        self.assertEqual("小雪", same["nickname"])
        self.assertEqual(1, host.saved)

    def test_auto_profile_gates_and_manual_disable_precedence(self) -> None:
        event = SimpleNamespace(unified_msg_origin="telegram:FriendMessage:10001")
        host = _AutoProfileHost()
        host.enable_auto_user_profile_creation = False
        self.assertEqual((None, False), host._ensure_auto_private_user_profile(event, user_id="10001"))

        host = _AutoProfileHost()
        host.enable_auto_user_profile_creation = True
        host.bot_ids.add("10001")
        self.assertEqual((None, False), host._ensure_auto_private_user_profile(event, user_id="10001"))

        host = _AutoProfileHost()
        host.enable_auto_user_profile_creation = True
        host.auto_profile_platforms = ["onebot"]
        self.assertEqual((None, False), host._ensure_auto_private_user_profile(event, user_id="10001"))

        self.assertFalse(host._is_target_private_user("10001", {"auto_enabled": True, "manual_disabled": True}))
        self.assertTrue(host._is_target_private_user("10001", {"auto_enabled": True, "manual_disabled": False}))

    def test_page_api_normalizes_new_profile_settings(self) -> None:
        method = _load_class_methods(
            "page_api_settings.py",
            "PageSettingNormalizerMixin",
            {"_normalize_page_core_setting"},
            {
                "Any": Any,
                "re": re,
                "relationship_stage_policy_json": lambda value: "normalized",
                "normalize_relationship_positive_stage_cap_key": lambda value: value,
                "normalize_normal_interaction_band_cap": lambda value: value,
                "_SETTING_UNHANDLED": object(),
            },
        )["_normalize_page_core_setting"]

        class Host:
            _normalize_page_core_setting = method
            plugin = SimpleNamespace()

            @staticmethod
            def _schema_bool_keys() -> set[str]:
                return set()

        host = Host()
        self.assertEqual(["onebot", "telegram"], host._normalize_page_core_setting("auto_profile_platforms", ["napcat", "TG", "unknown", "telegram"]))
        self.assertEqual("platform_display_name", host._normalize_page_core_setting("default_nickname_strategy", "owner"))
        self.assertEqual(0, host._normalize_page_core_setting("default_proactive_daily_limit", -8))

    def test_page_source_contains_current_relationship_projection_and_policy_editor(self) -> None:
        source = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "pages" / "陪伴面板" / "app.css").read_text(encoding="utf-8")
        html = (ROOT / "pages" / "陪伴面板" / "index.html").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_RELATIONSHIP_POLICY", source)
        self.assertIn("function renderRelationshipStatus", source)
        self.assertIn('name="relationship_stage_key"', source)
        self.assertIn('name="companion_intimacy"', source)
        self.assertIn(".relationship-stage-bar", css)
        self.assertIn('data-feature-open="${escapeHtml(key)}"', source)
        schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        self.assertIn("新用户最小档案", schema)
        self.assertIn("亲密度阶段策略", source)
        self.assertIn("用户档案", html)
        self.assertIn("记忆插件协同", html)

        editor_start = source.index("function relationshipPolicyEditor")
        editor_end = source.index("function bindRelationshipPolicyEditor", editor_start)
        self.assertNotIn("scoreGauge(", source[editor_start:editor_end])
        self.assertNotIn('type="range"', source[editor_start:editor_end])


if __name__ == "__main__":
    unittest.main()

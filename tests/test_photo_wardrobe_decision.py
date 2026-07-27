from __future__ import annotations

import sys
import unittest
from pathlib import Path


PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_private_companion import photo_wardrobe_decision
from astrbot_plugin_private_companion.photo_wardrobe_decision import (
    PhotoWardrobeDecision,
    analyze_photo_wardrobe,
    resolve_photo_wardrobe_decision,
)


class PhotoWardrobeDecisionTests(unittest.TestCase):
    def test_public_interface_is_limited_to_the_unified_decision_contract(self) -> None:
        self.assertEqual(
            photo_wardrobe_decision.__all__,
            [
                "PhotoWardrobeIntent",
                "PhotoWardrobeDecision",
                "analyze_photo_wardrobe",
                "resolve_photo_wardrobe_decision",
            ],
        )

    def test_explicit_scene_preset_overrides_prompt_and_reference_outfit(self) -> None:
        intent = analyze_photo_wardrobe(
            "换成校服，不要睡衣",
            requested_scene_preset="居家睡衣",
        )

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="换成校服，不要睡衣",
            intent=intent,
            reference={
                "id": "library-formal",
                "kind": "library",
                "path": "C:/images/formal.png",
                "reference_roles": ["identity", "outfit", "style"],
                "outfit_category": "formalwear",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：卧室；今日穿搭：白衬衫和长裙；当前场景：居家",
            base_prompt=(
                "Positive prompt: user request: 换成校服, visual continuity reference: "
                "今日穿搭：白衬衫和长裙, keep today's outfit and character appearance "
                "consistent with the reference image. Negative prompt: 睡衣."
            ),
            available_presets={"居家睡衣", "校服人像", "日常穿搭"},
        )

        self.assertEqual(decision.rule_id, "explicit_scene_preset")
        self.assertEqual(decision.category, "sleepwear")
        self.assertTrue(decision.lock_outfit)
        self.assertEqual(decision.authoritative_preset, "居家睡衣")
        self.assertEqual(decision.selected_presets, ("居家睡衣",))
        self.assertEqual(decision.effective_reference_roles, ("identity", "style"))
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertNotIn("today's outfit and character", decision.base_prompt.lower())
        self.assertIn("reference_outfit_role_removed", decision.adjustments)
        self.assertIn("daily_outfit_context_removed", decision.adjustments)
        self.assertIn("generated_daily_outfit_continuity_removed", decision.adjustments)

    def test_explicit_scene_preset_removes_uncategorized_reference_outfit_role(self) -> None:
        intent = analyze_photo_wardrobe("拍一张照片", requested_scene_preset="居家睡衣")
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="拍一张照片",
            intent=intent,
            requested_scene_preset="居家睡衣",
            reference={
                "id": "explicit_reference",
                "kind": "explicit",
                "path": "C:/images/reference.png",
                "reference_roles": ["identity", "outfit", "style"],
                "outfit_category": "",
                "outfit_lock_default": True,
            },
            available_presets={"居家睡衣"},
        )

        self.assertEqual(decision.rule_id, "explicit_scene_preset")
        self.assertEqual(decision.effective_reference_roles, ("identity", "style"))
        self.assertIn("reference_outfit_role_removed", decision.adjustments)

    def test_explicit_prompt_parses_traditional_format_and_overrides_locked_reference(self) -> None:
        prompt = (
            "Positive prompt: user request: change into a school uniform, classroom selfie. "
            "Negative prompt: sleepwear, formal attire."
        )
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="portrait",
            prompt_text=prompt,
            intent=intent,
            reference={
                "id": "daily_outfit",
                "kind": "daily_outfit",
                "path": "C:/images/today.png",
                "reference_roles": ["identity", "outfit"],
                "outfit_category": "daily_outfit",
                "outfit_lock_default": True,
            },
            scene_context="当前日程：上课；今日穿搭：针织衫和长裙；当前场景：教室",
            base_prompt=prompt,
            available_presets={"校服人像", "日常穿搭"},
        )

        self.assertEqual(intent.target_category, "school_uniform")
        self.assertEqual(intent.excluded_categories, ("sleepwear", "formalwear"))
        self.assertTrue(intent.change_requested)
        self.assertEqual(decision.rule_id, "explicit_prompt")
        self.assertEqual(decision.mode, "explicit_prompt")
        self.assertEqual(decision.category, "school_uniform")
        self.assertEqual(decision.preset_name, "校服人像")
        self.assertEqual(decision.selected_presets, ("校服人像",))
        self.assertEqual(decision.effective_reference_roles, ("identity",))
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertNotIn("今日穿搭", decision.scene_context)

    def test_explicit_prompt_removes_uncategorized_reference_outfit_role(self) -> None:
        prompt = "换成校服"
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference={
                "id": "explicit_reference",
                "kind": "explicit",
                "path": "C:/images/reference.png",
                "reference_roles": ["identity", "outfit", "pose"],
                "outfit_category": "",
                "outfit_lock_default": True,
            },
            available_presets={"校服人像"},
        )

        self.assertEqual(decision.rule_id, "explicit_prompt")
        self.assertEqual(decision.effective_reference_roles, ("identity", "pose"))
        self.assertIn("reference_outfit_role_removed", decision.adjustments)

    def test_custom_outfit_is_recognized_without_forcing_a_known_category(self) -> None:
        intent = analyze_photo_wardrobe("换成红色吊带长裙，别穿校服")

        self.assertEqual(intent.target_category, "custom_outfit")
        self.assertTrue(intent.custom_outfit)
        self.assertEqual(intent.excluded_categories, ("school_uniform",))

    def test_explicit_exclusion_removes_only_the_reference_outfit_role(self) -> None:
        prompt = "在卧室拍一张照片，不要睡衣"
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            reference={
                "id": "library-sleep",
                "kind": "library",
                "path": "C:/images/sleep.png",
                "reference_roles": ["identity", "outfit", "pose"],
                "outfit_category": "sleepwear",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：卧室；今日穿搭：睡衣；当前场景：睡前",
            base_prompt=prompt,
            available_presets={"角色自拍", "居家睡衣"},
        )

        self.assertEqual(intent.target_category, "")
        self.assertEqual(intent.excluded_categories, ("sleepwear",))
        self.assertEqual(decision.rule_id, "explicit_exclusion")
        self.assertEqual(decision.category, "")
        self.assertFalse(decision.lock_outfit)
        self.assertEqual(decision.effective_reference_roles, ("identity", "pose"))
        self.assertIn("reference_outfit_role_removed", decision.adjustments)

    def test_custom_exclusion_removes_matching_locked_reference_outfit_role(self) -> None:
        prompt = "在街边拍照，不要红色吊带长裙"
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            reference={
                "id": "library-red-dress",
                "kind": "library",
                "path": "C:/images/red-dress.png",
                "note": "红色吊带长裙，适合街拍",
                "reference_roles": ["identity", "outfit", "pose"],
                "outfit_category": "custom:红色吊带长裙",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：街边；当前场景：散步",
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(intent.excluded_categories, ())
        self.assertEqual(decision.rule_id, "explicit_exclusion")
        self.assertFalse(decision.lock_outfit)
        self.assertEqual(decision.effective_reference_roles, ("identity", "pose"))
        self.assertIn("reference_outfit_role_removed", decision.adjustments)

    def test_explicit_exclusion_removes_matching_daily_outfit_context_without_reference(self) -> None:
        prompt = "在卧室拍照，不要睡衣"
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference=None,
            scene_context="当前位置：卧室；今日穿搭：睡衣；当前场景：睡前",
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭", "居家睡衣"},
        )

        self.assertEqual(decision.rule_id, "explicit_exclusion")
        self.assertEqual(decision.category, "")
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertNotIn("日常穿搭", decision.selected_presets)
        self.assertIn("daily_outfit_context_removed", decision.adjustments)

    def test_custom_chinese_exclusion_removes_matching_daily_outfit_context(self) -> None:
        prompt = "在街边拍照，不要红色吊带长裙"
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            reference=None,
            scene_context="当前位置：街边；今日穿搭：红色吊带长裙；当前场景：散步",
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(intent.excluded_categories, ())
        self.assertEqual(intent.exclusion_text, "红色吊带长裙")
        self.assertEqual(decision.rule_id, "explicit_exclusion")
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertNotIn("日常穿搭", decision.selected_presets)

    def test_custom_english_exclusion_removes_matching_daily_outfit_context(self) -> None:
        prompt = "Take a street photo. Do not wear the red strappy maxi dress."
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            reference=None,
            scene_context=(
                "Current location: street; Today's outfit: red strappy maxi dress; "
                "Current scene: walking"
            ),
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(intent.excluded_categories, ())
        self.assertEqual(intent.exclusion_text, "wear the red strappy maxi dress")
        self.assertEqual(decision.rule_id, "explicit_exclusion")
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertNotIn("today's outfit", decision.scene_context.lower())
        self.assertIn("Current location: street", decision.scene_context)
        self.assertIn("Current scene: walking", decision.scene_context)
        self.assertNotIn("日常穿搭", decision.selected_presets)

    def test_home_scene_ignores_unrelated_exclusion_and_removes_daily_outfit_context(self) -> None:
        prompt = "在卧室拍照，不要校服"
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference=None,
            scene_context="当前位置：卧室；今日穿搭：睡衣；当前场景：睡前",
            base_prompt=(
                "Positive prompt: user request: 在卧室拍照, visual continuity reference: "
                "今日穿搭：睡衣, keep today's outfit and character appearance consistent "
                "with available visual continuity."
            ),
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(decision.rule_id, "no_wardrobe_source")
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertEqual(decision.selected_presets, ("角色自拍",))
        self.assertIn("daily_outfit_context_not_applicable", decision.adjustments)
        self.assertIn("daily_outfit_context_removed", decision.adjustments)
        self.assertIn("generated_daily_outfit_continuity_removed", decision.adjustments)
        self.assertNotIn("今日穿搭", decision.base_prompt)
        self.assertNotIn("today's outfit and character", decision.base_prompt.lower())
        self.assertIn("character identity", decision.base_prompt.lower())

    def test_current_outdoor_request_overrides_stale_home_context(self) -> None:
        prompt = "去公园拍一张自然自拍"

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference=None,
            scene_context="当前位置：卧室；今日穿搭：针织衫和长裙；当前场景：睡前",
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(decision.rule_id, "daily_outfit_context")
        self.assertFalse(decision.remove_daily_outfit_context)
        self.assertIn("今日穿搭", decision.scene_context)
        self.assertEqual(decision.selected_presets, ("日常穿搭",))

    def test_explicit_outfit_showcase_keeps_daily_outfit_context_at_home(self) -> None:
        prompt = "在卧室拍一张照片，给我看看今天的穿搭"
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            reference=None,
            scene_context="当前位置：卧室；今日穿搭：针织衫和长裙；当前场景：休息",
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(intent.target_category, "")
        self.assertEqual(decision.rule_id, "daily_outfit_context")
        self.assertFalse(decision.remove_daily_outfit_context)
        self.assertIn("今日穿搭", decision.scene_context)
        self.assertEqual(decision.selected_presets, ("日常穿搭",))

    def test_daily_outfit_reference_is_the_authoritative_fallback(self) -> None:
        intent = analyze_photo_wardrobe("在街边拍一张自然自拍")

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="在街边拍一张自然自拍",
            intent=intent,
            reference={
                "id": "daily_outfit",
                "kind": "daily_outfit",
                "path": "C:/images/today.png",
                "reference_roles": ["identity", "outfit"],
                "outfit_category": "daily_outfit",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：街边；今日穿搭：针织衫和长裙",
            base_prompt="在街边拍一张自然自拍",
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(decision.rule_id, "daily_outfit_reference")
        self.assertEqual(decision.mode, "daily_outfit")
        self.assertEqual(decision.source, "selected_reference")
        self.assertEqual(decision.category, "daily_outfit")
        self.assertTrue(decision.lock_outfit)
        self.assertFalse(decision.remove_daily_outfit_context)
        self.assertEqual(decision.preset_name, "日常穿搭")
        self.assertEqual(decision.selected_presets, ("日常穿搭",))
        self.assertEqual(decision.effective_reference_roles, ("identity", "outfit"))
        self.assertEqual(decision.adjustments, ())

    def test_daily_outfit_reference_does_not_lock_in_home_scene(self) -> None:
        prompt = "在卧室拍一张自然自拍"

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference={
                "id": "daily_outfit",
                "kind": "daily_outfit",
                "path": "C:/images/today.png",
                "reference_roles": ["identity", "outfit"],
                "outfit_category": "daily_outfit",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：卧室；今日穿搭：针织衫和长裙；当前场景：睡前",
            base_prompt=prompt,
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(decision.rule_id, "identity_only")
        self.assertEqual(decision.category, "")
        self.assertFalse(decision.lock_outfit)
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertEqual(decision.effective_reference_roles, ("identity",))
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertEqual(decision.selected_presets, ("角色自拍",))
        self.assertIn("daily_outfit_reference_not_applicable", decision.adjustments)
        self.assertIn("reference_outfit_role_removed", decision.adjustments)

    def test_recent_sent_photo_locks_outfit_and_cleans_advanced_schedule_context(self) -> None:
        prompt = "保持上一张的样子，换个坐姿"
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            reference={
                "id": "recent_sent_photo",
                "kind": "recent_sent_photo",
                "path": "C:/images/recent.png",
                "reference_roles": ["identity", "outfit", "scene", "continuity"],
                "outfit_category": "",
                "outfit_lock_default": True,
                "preferred_preset": "居家服",
            },
            scene_context="当前位置：书房；今日穿搭：通勤西装；当前场景：阅读",
            base_prompt=(
                "Positive prompt: user request: 保持上一张的样子，换个坐姿, "
                "keep today's outfit and character appearance consistent with available visual continuity."
            ),
            available_presets={"角色自拍", "居家服"},
        )

        self.assertEqual(decision.rule_id, "recent_photo_continuity")
        self.assertEqual(decision.mode, "continuity")
        self.assertEqual(decision.category, "reference_outfit")
        self.assertTrue(decision.lock_outfit)
        self.assertTrue(decision.remove_daily_outfit_context)
        self.assertEqual(decision.preset_name, "居家服")
        self.assertEqual(decision.selected_presets, ("居家服",))
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertNotIn("today's outfit and character", decision.base_prompt.lower())
        self.assertIn("daily_outfit_context_removed", decision.adjustments)
        self.assertIn("generated_daily_outfit_continuity_removed", decision.adjustments)

    def test_locked_library_reference_controls_the_complete_outfit(self) -> None:
        intent = analyze_photo_wardrobe("在卧室拍一张坐在床边的自拍")

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="在卧室拍一张坐在床边的自拍",
            intent=intent,
            reference={
                "id": "library-sleep",
                "kind": "library",
                "path": "C:/images/sleep.png",
                "reference_roles": ["identity", "outfit", "style"],
                "outfit_category": "sleepwear",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：卧室；今日穿搭：校服和外套；当前场景：睡前",
            base_prompt="在卧室拍一张坐在床边的自拍",
            available_presets={"角色自拍", "居家睡衣"},
        )

        self.assertEqual(decision.rule_id, "locked_reference_outfit")
        self.assertEqual(decision.mode, "reference_outfit")
        self.assertEqual(decision.category, "sleepwear")
        self.assertTrue(decision.lock_outfit)
        self.assertEqual(decision.preset_name, "居家睡衣")
        self.assertEqual(decision.selected_presets, ("居家睡衣",))
        self.assertEqual(decision.effective_reference_roles, ("identity", "outfit", "style"))
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertIn("daily_outfit_context_removed", decision.adjustments)

    def test_compatible_locked_reference_survives_unrelated_scene_exclusion(self) -> None:
        prompt = "保持上一张，不要睡衣"
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference={
                "id": "library-formal",
                "kind": "library",
                "path": "C:/images/formal.png",
                "reference_roles": ["identity", "outfit"],
                "outfit_category": "formalwear",
                "outfit_lock_default": True,
            },
            scene_context="当前位置：卧室；今日穿搭：睡衣；当前场景：睡前",
            base_prompt=prompt,
            available_presets={"礼服人像", "角色自拍"},
        )

        self.assertEqual(decision.rule_id, "locked_reference_outfit")
        self.assertEqual(decision.category, "formalwear")
        self.assertTrue(decision.lock_outfit)
        self.assertNotIn("今日穿搭", decision.scene_context)
        self.assertIn("daily_outfit_context_removed", decision.adjustments)

    def test_daily_outfit_context_is_a_soft_fallback_for_identity_reference(self) -> None:
        intent = analyze_photo_wardrobe("在公园拍一张自然自拍")

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="在公园拍一张自然自拍",
            intent=intent,
            reference={
                "id": "persona",
                "kind": "persona",
                "path": "C:/images/persona.png",
                "reference_roles": ["identity", "style"],
                "outfit_category": "",
                "outfit_lock_default": False,
            },
            scene_context="当前位置：公园；今日穿搭：针织衫和长裙；当前场景：散步",
            base_prompt="在公园拍一张自然自拍",
            available_presets={"角色自拍", "日常穿搭"},
        )

        self.assertEqual(decision.rule_id, "daily_outfit_context")
        self.assertEqual(decision.mode, "daily_outfit_context")
        self.assertEqual(decision.source, "daily_outfit")
        self.assertEqual(decision.category, "daily_outfit")
        self.assertFalse(decision.lock_outfit)
        self.assertEqual(decision.preset_name, "日常穿搭")
        self.assertEqual(decision.selected_presets, ("日常穿搭",))
        self.assertIn("今日穿搭", decision.scene_context)
        self.assertEqual(decision.effective_reference_roles, ("identity", "style"))

    def test_identity_reference_does_not_lock_incidental_clothing(self) -> None:
        intent = analyze_photo_wardrobe("拍一张头像特写")

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="拍一张头像特写",
            intent=intent,
            reference={
                "id": "persona",
                "kind": "persona",
                "path": "C:/images/persona.png",
                "reference_roles": ["identity", "style"],
                "outfit_category": "",
                "outfit_lock_default": False,
            },
            scene_context="当前位置：房间；当前场景：休息",
            base_prompt="拍一张头像特写",
            available_presets={"角色自拍", "头像特写"},
        )

        self.assertEqual(decision.rule_id, "identity_only")
        self.assertEqual(decision.mode, "identity_only")
        self.assertFalse(decision.lock_outfit)
        self.assertEqual(decision.category, "")
        self.assertEqual(decision.selected_presets, ("头像特写",))
        self.assertEqual(decision.effective_reference_roles, ("identity", "style"))

    def test_image_edit_keeps_the_source_contract_without_wardrobe_presets(self) -> None:
        prompt = "把外套改成校服"
        intent = analyze_photo_wardrobe(prompt)

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="edit",
            prompt_text=prompt,
            intent=intent,
            reference={
                "id": "explicit_reference",
                "kind": "source",
                "path": "C:/images/source.png",
                "reference_roles": ["source"],
            },
            scene_context="今日穿搭：针织衫",
            base_prompt=prompt,
            available_presets={"居家睡衣", "校服人像"},
        )

        self.assertEqual(decision.rule_id, "non_selfie_source_edit")
        self.assertEqual(decision.mode, "source_edit")
        self.assertEqual(decision.source, "explicit_reference")
        self.assertEqual(decision.category, "")
        self.assertFalse(decision.lock_outfit)
        self.assertEqual(decision.selected_presets, ())
        self.assertEqual(decision.effective_reference_roles, ("source",))

    def test_no_reference_returns_an_auditable_unlocked_decision(self) -> None:
        intent = analyze_photo_wardrobe("拍一张自然自拍")

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="拍一张自然自拍",
            intent=intent,
            reference=None,
            scene_context="当前位置：公园；当前场景：散步",
            base_prompt="拍一张自然自拍",
            available_presets={"角色自拍"},
        )

        self.assertEqual(decision.rule_id, "no_wardrobe_source")
        self.assertEqual(decision.mode, "none")
        self.assertEqual(decision.source, "none")
        self.assertEqual(decision.category, "")
        self.assertFalse(decision.lock_outfit)
        self.assertEqual(decision.selected_presets, ("角色自拍",))
        self.assertEqual(decision.reference_roles, ())

    def test_locked_decision_requires_a_wardrobe_category(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a category"):
            PhotoWardrobeDecision(rule_id="invalid_lock", lock_outfit=True)

    def test_effective_reference_roles_must_be_a_subset(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a subset"):
            PhotoWardrobeDecision(
                rule_id="invalid_roles",
                reference_roles=("identity",),
                effective_reference_roles=("identity", "outfit"),
            )

    def test_removed_daily_outfit_context_cannot_remain_in_decision(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not removed"):
            PhotoWardrobeDecision(
                rule_id="invalid_context",
                remove_daily_outfit_context=True,
                scene_context="今日穿搭：校服",
            )

    def test_non_daily_category_cannot_keep_conflicting_daily_outfit_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not removed"):
            PhotoWardrobeDecision(
                rule_id="invalid_non_daily_context",
                category="sleepwear",
                lock_outfit=True,
                scene_context="今日穿搭：校服",
            )

    def test_as_dict_keeps_legacy_log_keys_and_adds_audit_fields(self) -> None:
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="换成校服",
            intent=analyze_photo_wardrobe("换成校服"),
            reference=None,
            available_presets={"校服人像"},
        )

        payload = decision.as_dict()

        for key in (
            "mode",
            "source",
            "category",
            "lock_outfit",
            "remove_daily_outfit_context",
            "preset_name",
            "reference_image_path",
            "reference_id",
            "reference_kind",
            "reference_roles",
            "effective_reference_roles",
            "positive_instruction",
            "negative_instruction",
            "reason",
            "excluded_categories",
            "requested_outfit_text",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["decision_version"], 1)
        self.assertEqual(payload["rule_id"], "explicit_prompt")
        self.assertEqual(payload["selected_presets"], ["校服人像"])
        self.assertIn("adjustments", payload)

    def test_non_wardrobe_scene_preset_is_authoritative_without_locking_outfit(self) -> None:
        prompt = "拍一张头像特写"
        intent = analyze_photo_wardrobe(prompt, requested_scene_preset="头像特写")

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=intent,
            requested_scene_preset="头像特写",
            reference=None,
            available_presets={"角色自拍", "头像特写"},
        )

        self.assertEqual(intent.requested_preset_category, "")
        self.assertEqual(decision.authoritative_preset, "头像特写")
        self.assertEqual(decision.selected_presets, ("头像特写",))
        self.assertEqual(decision.category, "")
        self.assertFalse(decision.lock_outfit)

    def test_context_cleanup_preserves_neutral_identity_continuity(self) -> None:
        prompt = "换成校服"
        base_prompt = (
            "Positive prompt: user request: 换成校服, visual continuity reference: "
            "今日穿搭：针织衫和长裙, preserve character identity and stable appearance "
            "from available visual continuity. Negative prompt: watermark."
        )

        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text=prompt,
            intent=analyze_photo_wardrobe(prompt),
            reference=None,
            base_prompt=base_prompt,
            available_presets={"校服人像"},
        )

        self.assertNotIn("今日穿搭", decision.base_prompt)
        self.assertIn("preserve character identity", decision.base_prompt)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.companion_interaction_expression import (
    build_expression_decision,
    expression_decision_prompt,
)
from astrbot_plugin_private_companion.proactive import ProactiveMixin
from astrbot_plugin_private_companion.relationship_policy import default_relationship_stage_policy


class _ProactiveHarness(ProactiveMixin):
    max_daily_messages = 8
    enable_custom_relationship_stage_policy = False
    normal_interaction_band_cap = "warm"
    owner_exclusive_proactive_limit = 6

    def __init__(self) -> None:
        self.config = {}
        self.data = {"daily_state": {"energy": 70, "conditions": []}}

    @staticmethod
    def _private_user_role(_user, _user_id: str = "") -> str:
        return "friend"

    @staticmethod
    def _get_relevant_important_dates() -> list[dict]:
        return []

    @staticmethod
    def _proactive_intensity_effect(_key: str, default):
        return default


class RelationshipProactiveSoftTargetTests(unittest.TestCase):
    def test_acquaintance_starts_with_one_soft_target(self) -> None:
        stages = default_relationship_stage_policy()
        acquaintance = next(item for item in stages if item["key"] == "acquaintance")
        self.assertEqual(1, acquaintance["proactive_care_limit"])

    def test_non_distant_stage_target_does_not_cut_the_hard_daily_allowance(self) -> None:
        harness = _ProactiveHarness()
        user = {
            "relationship_role": "friend",
            "relationship_mode": "normal",
            "relationship_score": 0,
            "ignored_streak": 0,
        }

        self.assertEqual(8, harness._effective_user_daily_limit(user))
        self.assertEqual(1, harness._relationship_proactive_soft_target(user))
        self.assertLess(harness._soft_daily_target(user), 1.0)

        decision = build_expression_decision(
            {
                "relationship_score": 0,
                "relationship_baseline": {
                    "stage_key": "acquaintance",
                    "proactive_care_limit": 1,
                },
                "proactive_candidate": {"eligible": True, "budget": 8},
            }
        )
        self.assertEqual(8, decision.proactive_budget)
        self.assertEqual(1, decision.proactive_target)
        self.assertIn("relationship_proactive_soft_target", decision.reason_codes)
        prompt = expression_decision_prompt(decision)
        self.assertIn("柔性节奏目标", prompt)
        self.assertIn("不是必须凑满或一到即停的硬配额", prompt)

    def test_relationship_distance_and_explicit_zeroes_remain_hard_gates(self) -> None:
        harness = _ProactiveHarness()
        distant = {
            "relationship_role": "friend",
            "relationship_mode": "normal",
            "relationship_score": -1,
        }
        disabled = {**distant, "relationship_score": 0, "proactive_daily_limit": 0}

        self.assertEqual(0, harness._effective_user_daily_limit(distant))
        self.assertEqual(0, harness._effective_user_daily_limit(disabled))

        harness.max_daily_messages = 0
        self.assertEqual(0, harness._effective_user_daily_limit({**distant, "relationship_score": 0}))

    def test_cooldown_and_hurt_state_remain_hard_decision_gates(self) -> None:
        baseline = {
            "stage_key": "familiar",
            "proactive_care_limit": 2,
        }
        cooldown = build_expression_decision(
            {
                "relationship_score": 200,
                "relationship_baseline": baseline,
                "proactive_candidate": {
                    "eligible": True,
                    "budget": 8,
                    "current_ts": 100,
                    "cooldown_until": 200,
                },
            }
        )
        hurt = build_expression_decision(
            {
                "relationship_score": 200,
                "relationship_baseline": baseline,
                "current_interaction": {"expression_band": "hurt"},
                "proactive_candidate": {"eligible": True, "budget": 8},
            }
        )

        self.assertEqual(0, cooldown.proactive_budget)
        self.assertIn("proactive_cooldown_active", cooldown.reason_codes)
        self.assertEqual(0, hurt.proactive_budget)
        self.assertIn("interaction_proactive_suppressed", hurt.reason_codes)


if __name__ == "__main__":
    unittest.main()

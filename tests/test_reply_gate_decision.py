"""Exercise reply decisions without importing AstrBot or using real time."""

import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / "decision.py"
spec = importlib.util.spec_from_file_location("reply_gate_decision", SOURCE)
decision = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = decision
spec.loader.exec_module(decision)

Judgement = decision.Judgement
Decision = decision.Decision
Thresholds = decision.Thresholds
OptionalPolicy = decision.OptionalPolicy
OptionalBudget = decision.OptionalBudget
decide = decision.decide


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.thresholds = Thresholds()
        self.follow_up = Judgement(
            "to_bot",
            self.thresholds.relation,
            self.thresholds.response_needed,
            0.0,
            0.0,
        )
        self.open_question = Judgement(
            "open_group",
            self.thresholds.open_group,
            0.0,
            self.thresholds.interjection_welcome,
            0.0,
        )

    def test_default_thresholds(self):
        schema = json.loads(SOURCE.with_name("_conf_schema.json").read_text())
        self.assertEqual(
            vars(self.thresholds),
            {
                key: meta["default"]
                for key, meta in schema["thresholds"]["items"].items()
            },
        )

    def test_decision_string_values(self):
        for result in Decision:
            with self.subTest(result=result):
                self.assertEqual(str(result), result.name)

    def test_follow_up_at_both_thresholds_replies(self):
        self.assertEqual(decide(self.follow_up, self.thresholds), Decision.REPLY)

    def test_follow_up_below_either_threshold_skips(self):
        for changes in (
            {"relation_probability": self.thresholds.relation - 0.000001},
            {"response_needed": self.thresholds.response_needed - 0.000001},
        ):
            with self.subTest(changes=changes):
                judgement = replace(self.follow_up, **changes)
                self.assertEqual(decide(judgement, self.thresholds), Decision.SKIP)

    def test_follow_up_above_thresholds_replies(self):
        judgement = replace(
            self.follow_up, relation_probability=0.80, response_needed=0.94
        )
        self.assertEqual(decide(judgement, self.thresholds), Decision.REPLY)

    def test_follow_up_does_not_depend_on_optional_probability(self):
        budget = OptionalBudget()
        policy = OptionalPolicy(probability=0.0)
        self.assertFalse(budget.allow(0.0, policy, draw=lambda: 0.0))
        self.assertEqual(decide(self.follow_up, self.thresholds), Decision.REPLY)

    def test_failed_follow_up_does_not_fall_back_to_interjection(self):
        judgement = replace(
            self.follow_up, response_needed=0.0, interjection_welcome=1.0
        )
        self.assertEqual(decide(judgement, self.thresholds), Decision.SKIP)

    def test_open_question_at_both_thresholds_is_optional(self):
        self.assertEqual(decide(self.open_question, self.thresholds), Decision.OPTIONAL)

    def test_open_question_below_either_threshold_skips(self):
        for changes in (
            {"relation_probability": self.thresholds.open_group - 0.000001},
            {"interjection_welcome": self.thresholds.interjection_welcome - 0.000001},
        ):
            with self.subTest(changes=changes):
                judgement = replace(self.open_question, **changes)
                self.assertEqual(decide(judgement, self.thresholds), Decision.SKIP)

    def test_open_question_above_thresholds_remains_optional(self):
        judgement = replace(
            self.open_question,
            relation_probability=1.0,
            response_needed=1.0,
            interjection_welcome=1.0,
        )
        self.assertEqual(decide(judgement, self.thresholds), Decision.OPTIONAL)

    def test_others_and_unclear_always_skip(self):
        for relation in ("others", "unclear"):
            with self.subTest(relation=relation):
                judgement = Judgement(relation, 1.0, 1.0, 1.0, 1.0)
                self.assertEqual(decide(judgement, self.thresholds), Decision.SKIP)

    def test_confidence_does_not_change_decision(self):
        for judgement in (self.follow_up, self.open_question):
            with self.subTest(relation=judgement.relation):
                self.assertEqual(
                    decide(replace(judgement, confidence=0.0), self.thresholds),
                    decide(replace(judgement, confidence=1.0), self.thresholds),
                )

    def test_custom_thresholds_apply_to_each_branch(self):
        thresholds = Thresholds(0.90, 0.95, 0.92, 0.96)
        self.assertEqual(decide(self.follow_up, thresholds), Decision.SKIP)
        self.assertEqual(decide(self.open_question, thresholds), Decision.SKIP)
        self.assertEqual(
            decide(Judgement("to_bot", 0.90, 0.95, 0.0, 0.0), thresholds),
            Decision.REPLY,
        )
        self.assertEqual(
            decide(Judgement("open_group", 0.92, 0.0, 0.96, 0.0), thresholds),
            Decision.OPTIONAL,
        )


class OptionalBudgetTests(unittest.TestCase):
    def setUp(self):
        self.budget = OptionalBudget()
        self.policy = OptionalPolicy(
            probability=0.05, cooldown_seconds=90, max_replies=2
        )
        self.draw = Mock(return_value=0.0)

    def test_default_policy(self):
        schema = json.loads(SOURCE.with_name("_conf_schema.json").read_text())
        policy = OptionalPolicy()
        self.assertEqual(policy.probability, schema["optional_probability"]["default"])
        self.assertEqual(
            policy.cooldown_seconds, schema["optional_cooldown_seconds"]["default"]
        )
        self.assertEqual(policy.window_seconds, 600)
        self.assertEqual(policy.max_replies, schema["optional_max_replies"]["default"])

    def test_eligible_candidate_draws_once(self):
        self.assertTrue(self.budget.allow(0.0, self.policy, self.draw))
        self.draw.assert_called_once_with()

    def test_cooldown_blocks_without_drawing(self):
        self.budget.record_sent(0.0)
        self.assertFalse(self.budget.allow(89.999, self.policy, self.draw))
        self.draw.assert_not_called()

    def test_exact_cooldown_boundary_allows(self):
        self.budget.record_sent(0.0)
        self.assertTrue(self.budget.allow(90.0, self.policy, self.draw))
        self.draw.assert_called_once_with()

    def test_full_window_blocks_without_drawing(self):
        self.budget.record_sent(0.0)
        self.budget.record_sent(90.0)
        self.assertFalse(self.budget.allow(599.999, self.policy, self.draw))
        self.draw.assert_not_called()

    def test_exact_window_boundary_expires_oldest_send(self):
        self.budget.record_sent(0.0)
        self.budget.record_sent(90.0)
        self.assertTrue(self.budget.allow(600.0, self.policy, self.draw))
        self.budget.record_sent(600.0)
        self.assertTrue(self.budget.allow(690.0, self.policy, self.draw))
        self.assertEqual(self.draw.call_count, 2)

    def test_long_idle_period_expires_all_old_sends(self):
        self.budget.record_sent(0.0)
        self.budget.record_sent(90.0)
        self.assertTrue(self.budget.allow(1000.0, self.policy, self.draw))
        self.budget.record_sent(1000.0)
        self.assertTrue(self.budget.allow(1090.0, self.policy, self.draw))

    def test_allowed_but_unsent_candidates_do_not_use_budget(self):
        for _ in range(3):
            self.assertTrue(self.budget.allow(0.0, self.policy, self.draw))
        self.budget.record_sent(10.0)
        self.assertFalse(self.budget.allow(99.999, self.policy, self.draw))
        self.assertTrue(self.budget.allow(100.0, self.policy, self.draw))

    def test_probability_miss_does_not_use_budget(self):
        self.draw.side_effect = [0.90, 0.0]
        self.assertFalse(self.budget.allow(0.0, self.policy, self.draw))
        self.assertTrue(self.budget.allow(0.0, self.policy, self.draw))
        self.assertEqual(self.draw.call_count, 2)

    def test_probability_boundary(self):
        for draw_value, expected in ((0.049999, True), (0.05, False), (0.9, False)):
            with self.subTest(draw_value=draw_value):
                draw = Mock(return_value=draw_value)
                self.assertEqual(self.budget.allow(0.0, self.policy, draw), expected)
                draw.assert_called_once_with()

    def test_probability_zero_and_one(self):
        for probability, draw_value, expected in (
            (0.0, 0.0, False),
            (1.0, 0.999999, True),
        ):
            with self.subTest(probability=probability):
                policy = replace(self.policy, probability=probability)
                draw = Mock(return_value=draw_value)
                self.assertEqual(self.budget.allow(0.0, policy, draw), expected)
                draw.assert_called_once_with()

    def test_zero_reply_limit_blocks_without_drawing(self):
        policy = replace(self.policy, max_replies=0)
        self.assertFalse(self.budget.allow(0.0, policy, self.draw))
        self.draw.assert_not_called()

    def test_cooldown_longer_than_window_still_blocks(self):
        policy = replace(self.policy, cooldown_seconds=700)
        self.budget.record_sent(0.0)
        for now in (600.0, 699.999):
            with self.subTest(now=now):
                self.assertFalse(self.budget.allow(now, policy, self.draw))
        self.draw.assert_not_called()
        self.assertTrue(self.budget.allow(700.0, policy, self.draw))

    def test_group_budgets_are_independent(self):
        other_group = OptionalBudget()
        self.budget.record_sent(0.0)
        self.assertFalse(self.budget.allow(1.0, self.policy, self.draw))
        self.assertTrue(other_group.allow(1.0, self.policy, self.draw))
        self.draw.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

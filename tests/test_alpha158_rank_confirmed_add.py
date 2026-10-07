import unittest
from types import SimpleNamespace

import pandas as pd

from scripts.validate_alpha158_causal_rank_confirmed_add_sleeve_v1 import (
    filter_proposals,
)
from scripts.validate_alpha158_causal_industry_confirmed_add_sleeve_v1 import (
    filter_proposals as filter_industry_proposals,
)


class Alpha158RankConfirmedAddTest(unittest.TestCase):

    @staticmethod
    def _fixture():
        proposals = [
            SimpleNamespace(proposal_id="p1", evaluation_id="e1",
                            signal_asof=20250102, symbol="sz000001"),
            SimpleNamespace(proposal_id="p2", evaluation_id="e2",
                            signal_asof=20250102, symbol="sz000002"),
            SimpleNamespace(proposal_id="p3", evaluation_id="e3",
                            signal_asof=20250102, symbol="sz000003"),
        ]
        evaluations = [
            SimpleNamespace(evaluation_id="e1", evaluated_inputs={
                "benchmark_close": 4000.0, "benchmark_ma200": 3900.0}),
            SimpleNamespace(evaluation_id="e2", evaluated_inputs={
                "benchmark_close": 4000.0, "benchmark_ma200": 3900.0}),
            SimpleNamespace(evaluation_id="e3", evaluated_inputs={
                "benchmark_close": 3800.0, "benchmark_ma200": 3900.0}),
        ]
        scores = pd.DataFrame({
            "signal_asof": [20250102, 20250102, 20250102],
            "symbol": ["sz000001", "sz000002", "sz000003"],
            "daily_rank": [50, 51, 10],
        })
        return {"add_proposals": proposals,
                "policy_evaluations": evaluations}, scores

    def test_rank_gate_reuses_existing_entry_boundary(self):
        audit, scores = self._fixture()
        accepted, decisions = filter_proposals(
            audit, "R_entry_rank_confirmed", scores, 50)
        self.assertEqual([item.proposal_id for item in accepted], ["p1", "p3"])
        self.assertTrue(decisions[0]["rank_confirmed"])
        self.assertFalse(decisions[1]["rank_confirmed"])

    def test_market_gate_uses_only_same_signal_day_snapshot(self):
        audit, scores = self._fixture()
        accepted, _ = filter_proposals(
            audit, "RM_entry_rank_confirmed_market_up", scores, 50)
        self.assertEqual([item.proposal_id for item in accepted], ["p1"])

    def test_missing_rank_fails_closed(self):
        audit, scores = self._fixture()
        accepted, decisions = filter_proposals(
            audit, "R_entry_rank_confirmed",
            scores[scores.symbol.ne("sz000001")], 50)
        self.assertNotIn("p1", [item.proposal_id for item in accepted])
        self.assertIsNone(decisions[0]["daily_rank"])
class Alpha158IndustryConfirmedAddTest(unittest.TestCase):

    def test_industry_neutral_boundary_and_market_overlay(self):
        proposals = [
            SimpleNamespace(proposal_id="p1", evaluation_id="e1",
                            signal_asof=20250102, symbol="sz000001"),
            SimpleNamespace(proposal_id="p2", evaluation_id="e2",
                            signal_asof=20250102, symbol="sz000002"),
            SimpleNamespace(proposal_id="p3", evaluation_id="e3",
                            signal_asof=20250102, symbol="sz000003"),
        ]
        evaluations = [
            SimpleNamespace(evaluation_id="e1", evaluated_inputs={
                "benchmark_close": 4000.0, "benchmark_ma200": 3900.0}),
            SimpleNamespace(evaluation_id="e2", evaluated_inputs={
                "benchmark_close": 4000.0, "benchmark_ma200": 3900.0}),
            SimpleNamespace(evaluation_id="e3", evaluated_inputs={
                "benchmark_close": 3800.0, "benchmark_ma200": 3900.0}),
        ]
        audit = {"add_proposals": proposals,
                 "policy_evaluations": evaluations}
        panel = SimpleNamespace(
            dates=[20250102],
            symbol_index={"sz000001": 0, "sz000002": 1, "sz000003": 2})
        history = SimpleNamespace(values=lambda day: [0.0, -0.01, 0.02])
        accepted, decisions = filter_industry_proposals(
            audit, "I_industry_leader", panel, history, 0.0)
        self.assertEqual([item.proposal_id for item in accepted], ["p1", "p3"])
        self.assertTrue(decisions[0]["industry_confirmed"])
        self.assertFalse(decisions[1]["industry_confirmed"])
        market_accepted, _ = filter_industry_proposals(
            audit, "IM_industry_leader_market_up", panel, history, 0.0)
        self.assertEqual([item.proposal_id for item in market_accepted], ["p1"])


if __name__ == "__main__":
    unittest.main()

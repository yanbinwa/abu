"""M5 registered-baseline and historical decision state tests."""
import copy
import unittest

from abupy.MLBu.ABuMLContracts import ExperimentRegistration, MLContractError
from abupy.MLBu.ABuMLDecision import decide_historical_screen


def registration():
    return ExperimentRegistration(
        experiment_id="combiner", experiment_family_id="family",
        experiment_type="score_combiner", baseline_arm_id="all_mean_rank_a0",
        candidate_arm_ids=("combiner_a1",), config_sha256="a"*64,
        data_manifest_sha256="b"*64,
        expected_prediction_keys_sha256="c"*64,
        protected_file_hashes=(), created_at="2026-10-07T00:00:00+08:00")


def passing_evidence():
    row = {
        "cumulative_return_candidate": .15, "cumulative_return_baseline": .10,
        "max_drawdown_candidate": -.08, "max_drawdown_baseline": -.10,
        "es95_candidate": -.02, "es95_baseline": -.03,
        "three_limit_down_return_candidate": -.12,
        "three_limit_down_return_baseline": -.14,
        "average_exposure_candidate": .20, "average_exposure_baseline": .20,
        "annualized_uplift_pp": .8, "calmar_candidate": 1.2,
        "calmar_baseline": .8,
    }
    return {
        "candidate_arm_id": "combiner_a1",
        "baseline_arm_id": "all_mean_rank_a0",
        "baseline_reproduced": True, "data_and_leakage_audit_passed": True,
        "coverage_complete": True, "execution_consistency_passed": True,
        "cost_scenarios": {key: copy.deepcopy(row)
                           for key in ("25bp", "40bp", "60bp")},
        "bootstrap_20": {"annualized_return": {"lower": .001}},
        "bootstrap_40": {"annualized_return": {"lower": -100}},
        "bootstrap_60": {"annualized_return": {"lower": -100}},
        "top5_positive_profit_share": .5, "positive_year_count": 3,
    }


class DecisionTest(unittest.TestCase):

    def test_a1_cannot_use_ridge_baseline(self):
        evidence = passing_evidence()
        evidence["baseline_arm_id"] = "ridge_26f_v1"
        with self.assertRaises(MLContractError):
            decide_historical_screen(registration(), "combiner_a1", evidence)

    def test_diagnostic_blocks_do_not_change_pass(self):
        result = decide_historical_screen(
            registration(), "combiner_a1", passing_evidence())
        self.assertEqual(result.state, "PASS_HISTORICAL_SCREEN")

    def test_boundary_failure_and_inconclusive_are_unique(self):
        evidence = passing_evidence()
        evidence["top5_positive_profit_share"] = .5000001
        self.assertEqual(decide_historical_screen(
            registration(), "combiner_a1", evidence).state,
            "REJECT_HISTORICAL_SCREEN")
        evidence = passing_evidence()
        evidence["top5_positive_profit_share"] = None
        self.assertEqual(decide_historical_screen(
            registration(), "combiner_a1", evidence).state,
            "INCONCLUSIVE_HISTORICAL_SCREEN")
        evidence = passing_evidence()
        evidence["coverage_complete"] = False
        self.assertEqual(decide_historical_screen(
            registration(), "combiner_a1", evidence).state,
            "INCONCLUSIVE_INCOMPLETE_COVERAGE")

    def test_baseline_and_leakage_fail_closed(self):
        evidence = passing_evidence()
        evidence["baseline_reproduced"] = False
        self.assertEqual(decide_historical_screen(
            registration(), "combiner_a1", evidence).state,
            "REJECT_BASELINE_NOT_REPRODUCED")
        evidence = passing_evidence()
        evidence["data_and_leakage_audit_passed"] = False
        self.assertEqual(decide_historical_screen(
            registration(), "combiner_a1", evidence).state,
            "REJECT_DATA_OR_LEAKAGE_AUDIT")


if __name__ == "__main__":
    unittest.main()

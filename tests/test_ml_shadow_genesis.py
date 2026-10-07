"""M9 forward-shadow genesis admission and immutability tests."""
import tempfile
import unittest
from pathlib import Path

from abupy.MLBu.ABuMLContracts import (
    ExperimentDecision, ExperimentRegistration, MLContractError,
)
from abupy.MLBu.ABuMLShadowGenesis import (
    build_shadow_genesis, verify_shadow_genesis, write_shadow_genesis,
)


def registration():
    return ExperimentRegistration(
        experiment_id="experiment", experiment_family_id="family",
        experiment_type="score_combiner", baseline_arm_id="all_mean_rank_a0",
        candidate_arm_ids=("all_mean_rank_a1",), config_sha256="a"*64,
        data_manifest_sha256="b"*64,
        expected_prediction_keys_sha256="c"*64,
        protected_file_hashes=(), created_at="2026-10-07T00:00:00+08:00")


def decision(state="PASS_HISTORICAL_SCREEN"):
    return ExperimentDecision(
        experiment_id="experiment", candidate_arm_id="all_mean_rank_a1",
        baseline_arm_id="all_mean_rank_a0", state=state,
        evidence_sha256="d"*64)


class ShadowGenesisTest(unittest.TestCase):

    def test_rejected_or_wrong_baseline_cannot_register(self):
        with self.assertRaises(MLContractError):
            build_shadow_genesis(
                registration(), decision("REJECT_HISTORICAL_SCREEN"), [])
        wrong = ExperimentDecision(
            experiment_id="experiment", candidate_arm_id="all_mean_rank_a1",
            baseline_arm_id="ridge_26f_v1", state="PASS_HISTORICAL_SCREEN",
            evidence_sha256="d"*64)
        with self.assertRaises(MLContractError):
            build_shadow_genesis(registration(), wrong, [])

    def test_equal_empty_accounts_and_no_live_channels(self):
        payload = build_shadow_genesis(registration(), decision(), [])
        self.assertEqual(payload["accounts"]["baseline"]["initial_cash"],
                         payload["accounts"]["candidate"]["initial_cash"])
        self.assertEqual(payload["accounts"]["baseline"]["positions"], [])
        self.assertFalse(payload["protocol"]["formal_order_channel_enabled"])
        self.assertFalse(payload["protocol"]["trade_notification_enabled"])

    def test_write_is_immutable_and_artifact_change_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root/"model.txt"
            artifact.write_text("frozen", encoding="utf-8")
            payload = build_shadow_genesis(
                registration(), decision(), [artifact])
            path = write_shadow_genesis(root/"genesis.json", payload)
            verify_shadow_genesis(path)
            with self.assertRaises(FileExistsError):
                write_shadow_genesis(path, payload)
            artifact.write_text("changed", encoding="utf-8")
            with self.assertRaises(MLContractError):
                verify_shadow_genesis(path)


if __name__ == "__main__":
    unittest.main()

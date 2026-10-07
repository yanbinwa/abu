"""M1 immutable ML experiment registration tests."""
import copy
import tempfile
import unittest
from pathlib import Path

from abupy.MLBu.ABuMLContracts import MLContractError, load_experiment_config
from abupy.MLBu.ABuMLExperimentRegistry import (
    audit_registration, register_experiment,
)


class MLExperimentRegistryTest(unittest.TestCase):

    def setUp(self):
        self.config = load_experiment_config(
            "configs/selection/alpha158_ml_factor_optimization_v1.json")
        self.keys = [("2026-01-02T15:00:00+08:00", "sh600000", "f0",
                      "elastic_net_26f_v1")]

    def test_registration_is_immutable_and_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            protected = Path(directory) / "formal_account.json"
            protected.write_text('{"cash":1}\n', encoding="utf-8")
            output = Path(directory) / "experiment"
            first = register_experiment(
                output, self.config, "a" * 64, self.keys,
                protected_paths=[protected],
                created_at="2026-10-07T00:00:00+00:00")
            self.assertEqual(first["baseline_arm_id"], "ridge_26f_v1")
            with self.assertRaises(FileExistsError):
                register_experiment(output, self.config, "a" * 64, self.keys)
            audited = audit_registration(output, self.config, "a" * 64,
                                          self.keys)
            self.assertEqual(audited["registration_sha256"],
                             first["registration_sha256"])

    def test_config_data_keys_and_protected_files_are_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            protected = Path(directory) / "formal_account.json"
            protected.write_text('{"cash":1}\n', encoding="utf-8")
            output = Path(directory) / "experiment"
            register_experiment(output, self.config, "a" * 64, self.keys,
                                protected_paths=[protected])
            changed = copy.deepcopy(self.config)
            changed["output_root"] += "-changed"
            with self.assertRaisesRegex(MLContractError, "config changed"):
                audit_registration(output, changed, "a" * 64, self.keys)
            with self.assertRaisesRegex(MLContractError, "data manifest"):
                audit_registration(output, self.config, "b" * 64, self.keys)
            with self.assertRaisesRegex(MLContractError, "prediction keys"):
                audit_registration(output, self.config, "a" * 64,
                                   self.keys + [("x", "y", "z", "q")])
            protected.write_text('{"cash":0}\n', encoding="utf-8")
            with self.assertRaisesRegex(MLContractError, "protected file"):
                audit_registration(output, self.config, "a" * 64, self.keys)


if __name__ == "__main__":
    unittest.main()

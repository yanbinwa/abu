"""M0 contract, configuration, and dependency fail-close tests."""
import copy
import unittest

from abupy.MLBu.ABuMLContracts import (
    MLContractError, MLDependencyUnavailable, assert_model_dependencies,
    canonical_config_sha256, load_experiment_config, load_model_registry,
    validate_experiment_config,
)


class MLContractsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.registry = load_model_registry()
        cls.replacement = load_experiment_config(
            "configs/selection/alpha158_ml_factor_optimization_v1.json")
        cls.combiner = load_experiment_config(
            "configs/selection/alpha158_all_mean_ml_combiner_v1.json")

    def test_canonical_hash_is_stable_and_sensitive(self):
        reordered = dict(reversed(list(self.replacement.items())))
        self.assertEqual(canonical_config_sha256(self.replacement),
                         canonical_config_sha256(reordered))
        changed = copy.deepcopy(self.replacement)
        changed["output_root"] += "_changed"
        self.assertNotEqual(canonical_config_sha256(self.replacement),
                            canonical_config_sha256(changed))

    def test_unknown_or_missing_fields_fail_closed(self):
        unknown = copy.deepcopy(self.replacement)
        unknown["surprise"] = True
        with self.assertRaisesRegex(MLContractError, "schema violation"):
            validate_experiment_config(unknown, model_registry=self.registry)
        missing = copy.deepcopy(self.replacement)
        del missing["risk_policy_id"]
        with self.assertRaisesRegex(MLContractError, "schema violation"):
            validate_experiment_config(missing, model_registry=self.registry)

    def test_experiment_family_controls_baseline_identity(self):
        self.assertEqual(self.replacement["baseline_arm_id"], "ridge_26f_v1")
        self.assertEqual(self.combiner["baseline_arm_id"],
                         "all_mean_rank_a0")
        wrong = copy.deepcopy(self.combiner)
        wrong["baseline_arm_id"] = "ridge_26f_v1"
        with self.assertRaisesRegex(MLContractError, "requires baseline_arm_id"):
            validate_experiment_config(wrong, model_registry=self.registry)

    def test_unregistered_models_and_fallbacks_are_forbidden(self):
        unknown = copy.deepcopy(self.replacement)
        unknown["candidate_models"][0] = "unknown_model"
        with self.assertRaisesRegex(MLContractError, "unregistered models"):
            validate_experiment_config(unknown, model_registry=self.registry)
        fallback = copy.deepcopy(self.registry)
        fallback["models"]["lambdarank_26f_v1"]["fallback"] = "ridge_26f_v1"
        with self.assertRaisesRegex(MLContractError, "fallback is forbidden"):
            validate_experiment_config(self.replacement,
                                       model_registry=fallback)

    def test_missing_lightgbm_reports_dependency_without_fallback(self):
        versions = {
            "numpy": "1.23.5", "scikit-learn": "1.2.2",
            "scipy": "1.10.1", "lightgbm": "not-installed",
        }
        with self.assertRaisesRegex(MLDependencyUnavailable,
                                    "dependency is not installed"):
            assert_model_dependencies(["lambdarank_26f_v1"], self.registry,
                                      versions)
        self.assertTrue(assert_model_dependencies(
            ["ridge_26f_v1"], self.registry, versions))


if __name__ == "__main__":
    unittest.main()

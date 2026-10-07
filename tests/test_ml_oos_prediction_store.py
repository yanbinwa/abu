"""M1 OOS provenance, recursive manifest, and exact coverage tests."""
import unittest

from abupy.MLBu.ABuMLContracts import (
    MLContractError, ModelSourceManifest, OOSPredictionRecord,
)
from abupy.MLBu.ABuMLOOSPredictionStore import (
    IncompletePredictionCoverage, MLOOSPredictionStore,
    ModelSourceManifestCatalog,
)


def source(manifest_id="source-f0", candidate="candidate", model="model",
           fold="f0", parents=(), hashes=(), **changes):
    payload = {
        "manifest_id": manifest_id,
        "strategy_id": "mock", "candidate_arm_id": candidate,
        "model_id": model, "fold_id": fold,
        "feature_view_id": "features", "label_contract_id": "label20",
        "config_sha256": "a" * 64,
        "train_signal_max": "2025-09-01T15:00:00+08:00",
        "train_label_end_max": "2025-09-29T15:00:00+08:00",
        "train_label_available_at_max": "2025-09-29T15:05:00+08:00",
        "validation_start_asof": "2025-09-30T15:00:00+08:00",
        "validation_signal_max": "2025-12-01T15:00:00+08:00",
        "validation_label_end_max": "2025-12-29T15:00:00+08:00",
        "validation_label_available_at_max": "2025-12-29T15:05:00+08:00",
        "model_selected_at": "2025-12-29T15:06:00+08:00",
        "model_available_at": "2025-12-29T15:07:00+08:00",
        "prediction_block_start": "2025-12-30T15:00:00+08:00",
        "prediction_block_end": "2026-03-31T15:00:00+08:00",
        "parent_manifest_ids": tuple(parents),
        "parent_manifest_hashes": tuple(hashes),
    }
    payload.update(changes)
    return ModelSourceManifest(**payload)


def prediction(symbol="sh600000"):
    return OOSPredictionRecord(
        strategy_id="mock", signal_asof="2026-01-02T15:00:00+08:00",
        symbol=symbol, score=0.5, model_id="model", fold_id="f0",
        candidate_arm_id="candidate", source_manifest_id="source-f0",
        feature_view_id="features", label_contract_id="label20")


class MLOOSPredictionStoreTest(unittest.TestCase):

    def test_legal_train_signal_but_unmature_train_label_is_rejected(self):
        with self.assertRaisesRegex(MLContractError,
                                    "training labels are not mature"):
            source(train_label_available_at_max=
                   "2025-10-01T15:05:00+08:00")

    def test_validation_selection_information_cannot_cross_prediction(self):
        with self.assertRaisesRegex(MLContractError,
                                    "validation selection is not mature"):
            source(model_available_at="2026-01-02T15:01:00+08:00")

    def test_recursive_parent_hash_is_verified(self):
        catalog = ModelSourceManifestCatalog()
        parent = source("parent", candidate="family", model="family-model")
        catalog.add(parent)
        child = source(parents=("parent",), hashes=("0" * 64,))
        catalog.add(child)
        with self.assertRaisesRegex(MLContractError, "parent manifest hash"):
            catalog.audit(child.manifest_id)

    def test_exact_expected_key_coverage_is_required(self):
        record = prediction()
        catalog = ModelSourceManifestCatalog()
        catalog.add(source())
        expected = [record.key,
                    (record.signal_asof, "sz000001", "f0", "candidate")]
        store = MLOOSPredictionStore(expected, catalog)
        store.add(record)
        with self.assertRaisesRegex(IncompletePredictionCoverage,
                                    "differ from preregistered"):
            store.finalize()
        store.add(prediction("sz000001"))
        self.assertEqual(len(store.finalize()), 2)

    def test_duplicate_unexpected_and_unavailable_predictions_fail(self):
        record = prediction()
        catalog = ModelSourceManifestCatalog()
        catalog.add(source())
        store = MLOOSPredictionStore([record.key], catalog)
        store.add(record)
        with self.assertRaisesRegex(MLContractError, "duplicate"):
            store.add(record)
        other = prediction("sz000001")
        with self.assertRaisesRegex(MLContractError, "unexpected"):
            store.add(other)

    def test_overlapping_prediction_folds_are_rejected(self):
        catalog = ModelSourceManifestCatalog()
        catalog.add(source())
        with self.assertRaisesRegex(MLContractError, "folds overlap"):
            catalog.add(source(
                manifest_id="source-f1", fold="f1",
                prediction_block_start="2026-03-01T15:00:00+08:00",
                prediction_block_end="2026-05-31T15:00:00+08:00"))


if __name__ == "__main__":
    unittest.main()

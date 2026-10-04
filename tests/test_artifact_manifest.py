"""Content-addressed v4 artifact manifest tests."""
import json
import tempfile
import unittest
from pathlib import Path

from abupy.AlphaBu.ABuArtifactManifest import (
    CONFIG_COMPONENTS, artifact_file_record, bind_prediction_manifest,
    build_artifact_manifest, manifest_sha256, read_artifact_manifest,
    sha256_file, sha256_payload, verify_artifact_manifest,
    verify_prediction_artifact, write_artifact_manifest,
)
from abupy.AlphaBu.ABuTrialRegistry import (
    read_trial_registry, register_trial,
)


def hash_value(seed):
    return sha256_payload({"seed": seed})


def inputs(directory):
    directory = Path(directory)
    source = directory / "source.json"
    prediction = directory / "prediction.csv"
    source.write_text('{"source":1}\n', encoding="utf-8")
    prediction.write_text("date,symbol,score\n20260101,a,0.1\n", encoding="utf-8")
    snapshot_file = directory / "snapshot.bin"
    snapshot_file.write_bytes(b"snapshot-v1")
    snapshot = {
        "snapshot_id": "sha256:" + hash_value("snapshot"),
        "files": [{
            "path": "snapshot.bin",
            "sha256": sha256_file(snapshot_file),
            "bytes": snapshot_file.stat().st_size,
        }],
    }
    configs = {name: hash_value(name) for name in CONFIG_COMPONENTS}
    dates = {
        "train": ["2022-01-01", "2024-12-31"],
        "calibration": ["2025-01-01", "2025-06-30"],
        "test": ["2025-07-01", "2025-09-30"],
    }
    return source, prediction, snapshot, configs, dates


class ArtifactManifestTest(unittest.TestCase):

    def test_manifest_hash_is_independent_of_dictionary_order(self):
        left = {"b": 2, "a": {"y": 2, "x": 1}}
        right = {"a": {"x": 1, "y": 2}, "b": 2}
        self.assertEqual(sha256_payload(left), sha256_payload(right))

    def test_prediction_change_is_detected_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            source, prediction, snapshot, configs, dates = inputs(directory)
            manifest = bind_prediction_manifest(
                prediction, "prediction-test", directory, snapshot, configs,
                dates, source_files=[artifact_file_record("source", source)],
                git={"commit": "a" * 40, "branch": "test", "dirty": False,
                     "status_sha256": hash_value("clean")},
                environment={"python": "test", "implementation": "test",
                             "platform": "test", "dependencies": {}},
                created_at_utc="2026-10-03T00:00:00+00:00",
            )
            verified = verify_prediction_artifact(prediction)
            self.assertEqual(verified["manifest_sha256"],
                             manifest["manifest_sha256"])
            prediction.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_prediction_artifact(prediction)

    def test_snapshot_config_and_parent_expectations_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source, prediction, snapshot, configs, dates = inputs(directory)
            manifest = build_artifact_manifest(
                "artifact", "research", directory, snapshot, configs, dates,
                input_files=[artifact_file_record("source", source)],
                output_files=[artifact_file_record("prediction", prediction)],
                parent_manifest_sha256=hash_value("parent"),
                git={"commit": "a" * 40, "branch": "test", "dirty": False,
                     "status_sha256": hash_value("clean")},
                environment={"python": "test", "implementation": "test",
                             "platform": "test", "dependencies": {}},
                created_at_utc="2026-10-03T00:00:00+00:00",
            )
            with self.assertRaisesRegex(ValueError, "data snapshot"):
                verify_artifact_manifest(manifest, expected={
                    "data_snapshot_id": "sha256:" + hash_value("wrong")})
            with self.assertRaisesRegex(ValueError, "config hash"):
                verify_artifact_manifest(manifest, expected={
                    "config_hashes": {"model": hash_value("wrong")}})
            with self.assertRaisesRegex(ValueError, "expectation"):
                verify_artifact_manifest(manifest, expected={
                    "parent_manifest_sha256": hash_value("wrong")})

    def test_relocated_file_with_same_content_can_be_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            source, prediction, snapshot, configs, dates = inputs(directory)
            manifest = build_artifact_manifest(
                "relocated", "research", directory, snapshot, configs, dates,
                input_files=[artifact_file_record("source", source)],
                output_files=[artifact_file_record("prediction", prediction)],
                git={"commit": "a" * 40, "branch": "test", "dirty": False,
                     "status_sha256": hash_value("clean")},
                environment={"python": "test", "implementation": "test",
                             "platform": "test", "dependencies": {}},
                created_at_utc="2026-10-03T00:00:00+00:00",
            )
            moved = Path(directory) / "moved.csv"
            prediction.rename(moved)
            verified = verify_artifact_manifest(
                manifest, file_overrides={"prediction": moved})
            self.assertEqual(verified["artifact_id"], "relocated")

    def test_manifest_write_is_immutable_and_registry_links_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            source, prediction, snapshot, configs, dates = inputs(directory)
            manifest = build_artifact_manifest(
                "immutable", "research", directory, snapshot, configs, dates,
                input_files=[artifact_file_record("source", source)],
                output_files=[artifact_file_record("prediction", prediction)],
                git={"commit": "a" * 40, "branch": "test", "dirty": False,
                     "status_sha256": hash_value("clean")},
                environment={"python": "test", "implementation": "test",
                             "platform": "test", "dependencies": {}},
                created_at_utc="2026-10-03T00:00:00+00:00",
            )
            path = Path(directory) / "manifest.json"
            write_artifact_manifest(path, manifest)
            self.assertEqual(read_artifact_manifest(path), manifest)
            with self.assertRaises(FileExistsError):
                write_artifact_manifest(path, manifest)

            registry = Path(directory) / "registry.jsonl"
            register_trial(
                registry, "D0", "data", {"v": 1},
                manifest_sha256=manifest_sha256(manifest),
            )
            register_trial(
                registry, "D1", "factor", {"v": 1},
                manifest_sha256=manifest_sha256(manifest),
                parent_trial_id="D0",
            )
            records = read_trial_registry(registry)
            self.assertEqual(records[1]["parent_trial_id"], "D0")
            with self.assertRaisesRegex(ValueError, "not registered"):
                register_trial(
                    registry, "B2", "baseline", {"v": 1},
                    parent_trial_id="missing",
                )


if __name__ == "__main__":
    unittest.main()

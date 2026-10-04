import tempfile
import unittest
from pathlib import Path

from abupy.AlphaBu.ABuSourceSnapshot import (
    build_source_snapshot, write_source_snapshot,
)


class SourceSnapshotTest(unittest.TestCase):

    def test_current_checkout_snapshot_is_stable_and_dirty(self):
        root = Path(__file__).parents[1]
        first = build_source_snapshot(root)
        second = build_source_snapshot(root)
        self.assertEqual(first["source_snapshot_hash"],
                         second["source_snapshot_hash"])
        self.assertEqual(64, len(first["source_snapshot_hash"]))
        self.assertIsInstance(first["dirty"], bool)
        self.assertTrue(any(item["path"].endswith(".py")
                            for item in first["files"]))

    def test_snapshot_evidence_is_immutable(self):
        root = Path(__file__).parents[1]
        snapshot = build_source_snapshot(root)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source_snapshot.json"
            write_source_snapshot(target, snapshot)
            with self.assertRaises(FileExistsError):
                write_source_snapshot(target, snapshot)


if __name__ == "__main__":
    unittest.main()

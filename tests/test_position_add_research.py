"""Baseline freezing and canonical golden comparison tests."""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.compare_position_add_golden import compare_frames
from scripts.freeze_position_add_baseline import collect_path


class PositionAddResearchTest(unittest.TestCase):

    def test_path_hash_is_stable_and_changes_with_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.txt").write_text("b", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")
            first = collect_path("fixture", root)
            second = collect_path("fixture", root)
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            (root / "a.txt").write_text("changed", encoding="utf-8")
            third = collect_path("fixture", root)
            self.assertNotEqual(first["content_sha256"], third["content_sha256"])

    def test_comparator_sorts_by_business_key_and_uses_tolerance(self):
        baseline = pd.DataFrame({
            "date": [2, 1], "symbol": ["b", "a"],
            "quantity": [100, 200], "cash": [10.0, 20.0],
        })
        candidate = pd.DataFrame({
            "date": [1, 2], "symbol": ["a", "b"],
            "quantity": [200, 100], "cash": [20.0 + 1e-10, 10.0],
            "new_lineage": ["x", "y"],
        })
        result = compare_frames(
            baseline, candidate, keys=("date", "symbol"),
            float_abs_tolerance=1e-8,
        )
        self.assertTrue(result["equal"])

    def test_comparator_reports_first_material_difference(self):
        baseline = pd.DataFrame({"date": [1], "quantity": [100]})
        candidate = pd.DataFrame({"date": [1], "quantity": [200]})
        result = compare_frames(baseline, candidate, keys=("date",))
        self.assertFalse(result["equal"])
        self.assertEqual(result["differences"][0]["column"], "quantity")

    def test_nested_float_values_use_tolerance(self):
        baseline = pd.DataFrame([{"id": "a", "stress": {"gap": 1.0}}])
        candidate = pd.DataFrame([
            {"id": "a", "stress": {"gap": 1.0 + 1e-10}}])
        result = compare_frames(baseline, candidate, keys=("id",))
        self.assertTrue(result["equal"])


if __name__ == "__main__":
    unittest.main()

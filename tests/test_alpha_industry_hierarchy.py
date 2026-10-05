import unittest
from types import SimpleNamespace

import numpy as np

from abupy.AlphaBu.ABuAlphaIndustryHierarchy import (
    IndustryHierarchyConfig, IndustryHierarchyOverlay,
)


class FakeHistory:
    def __init__(self, panel, snapshots):
        self.panel = panel
        self.config = IndustryHierarchyConfig()
        self.snapshots = snapshots

    def snapshot(self, day):
        return self.snapshots[day]


class IndustryHierarchyOverlayTest(unittest.TestCase):
    def setUp(self):
        self.panel = SimpleNamespace(
            dates=np.array([20240102, 20240109]),
            symbols=["a", "b", "c", "d"],
            symbol_index={"a": 0, "b": 1, "c": 2, "d": 3})
        self.snapshot = dict(
            industry=np.array([1, 2, 1, 3]),
            industry_percentile=np.array([.8, .9, .8, .1]),
            leader_percentile=np.array([.8, .2, .9, .9]))
        self.executor = SimpleNamespace(positions={})

    def overlay(self, variant, snapshots=None):
        history = FakeHistory(
            self.panel, snapshots or {0: self.snapshot, 1: self.snapshot})
        return IndustryHierarchyOverlay(history, variant)

    def test_bottom_gate_preserves_alpha_order(self):
        overlay = self.overlay("H1_bottom20_gate")
        exits, entries = overlay.filter_review(
            self.panel, self.executor, 0, ["held"], ["a", "b", "d", "c"])
        self.assertEqual(exits, [])
        self.assertEqual(entries, ["a", "b", "c"])

    def test_industry_first_reorders_then_filters(self):
        overlay = self.overlay("H2_top30_industry_first")
        _, entries = overlay.filter_review(
            self.panel, self.executor, 0, [], ["a", "b", "d", "c"])
        self.assertEqual(entries, ["b", "a", "c"])

    def test_leader_requires_two_reviews(self):
        overlay = self.overlay("H3_persistent_leader")
        self.assertEqual(overlay.filter_review(
            self.panel, self.executor, 0, [], ["a", "c"])[1], [])
        self.assertEqual(overlay.filter_review(
            self.panel, self.executor, 1, [], ["a", "c"])[1], ["c", "a"])

    def test_industry_cap_counts_existing_holdings(self):
        overlay = self.overlay("H4_industry_cap")
        overlay.leader_streak = {"a": 1, "c": 1}
        self.executor.positions = {"existing": object()}
        self.panel.symbols.append("existing")
        self.panel.symbol_index["existing"] = 4
        snap = {name: np.append(value, value[0])
                for name, value in self.snapshot.items()}
        overlay.history.snapshots[0] = snap
        _, entries = overlay.filter_review(
            self.panel, self.executor, 0, [], ["a", "c"])
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()

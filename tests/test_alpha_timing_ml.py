import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlphaTimingML import (
    ENTRY_FEATURES, EntryMetaOverlay, ExitContinuationOverlay,
    TimingMLConfig, YearBoundaryLogisticModel,
    add_entry_tail_risk_labels, add_holding_protective_exit_labels,
)


class _ProbabilityModel:
    def __init__(self, values):
        self.values = values

    def probability(self, date, values):
        return self.values.get((int(date), values.get("symbol", "")),
                               self.values.get(int(date), np.nan))


class _Panel:
    dates = np.array([20240102+i for i in range(100)], dtype=int)
    symbols = ["A"]
    symbol_index = {"A": 0}
    close = np.linspace(10, 12, 100).reshape(-1, 1)
    open = close.copy()
    low = close-.1
    high = close+.1
    ma60 = np.linspace(9.5, 11.5, 100).reshape(-1, 1)
    atr21 = np.full((100, 1), .2)
    benchmark_close = np.linspace(100, 105, 100)


class _State:
    entry_day = 60
    initial_stop_adjusted = 9.0
    initial_r_adjusted = 1.0
    peak_close_adjusted = 11.5
    current_stop_adjusted = 9.5
    trailing_enabled = True


class AlphaTimingMLTest(unittest.TestCase):
    def test_year_model_purges_unmatured_labels(self):
        config = TimingMLConfig(minimum_training_rows=40,
                                minimum_class_rows=10)
        rows = []
        for index in range(60):
            label = index % 2
            rows.append({
                "date": 20231201+index % 20,
                "label_end": 20231220+index % 10,
                "label": label, "group": "g{}".format(index % 15),
                **{name: float(index+offset)
                   for offset, name in enumerate(ENTRY_FEATURES)},
            })
        # These rows must not enter the 2024 fit even though their decision is old.
        for index in range(20):
            rows.append({
                "date": 20231215, "label_end": 20240105,
                "label": index % 2, "group": "late{}".format(index),
                **{name: float(index+offset)
                   for offset, name in enumerate(ENTRY_FEATURES)},
            })
        model = YearBoundaryLogisticModel(ENTRY_FEATURES, config).fit(
            pd.DataFrame(rows), (2024,), "group")
        self.assertEqual(model.manifest[0]["training_rows"], 60)
        self.assertLess(model.manifest[0]["latest_label_end"], 20240101)

    def test_entry_overlay_only_passes_probability_threshold(self):
        features = pd.DataFrame([
            {"date": 20240102, "symbol": symbol,
             **{name: 0.0 for name in ENTRY_FEATURES}}
            for symbol in ("A", "B")])
        model = _ProbabilityModel({20240102: .7})
        overlay = EntryMetaOverlay(model, features)
        allowed = overlay.filter_entries(_Panel(), None, 0, ["A", "B"])
        self.assertEqual(allowed, ["A", "B"])
        self.assertTrue(all(row["allowed"] for row in overlay.decisions))

    def test_year_model_can_skip_insufficient_year_without_lowering_floor(self):
        config = TimingMLConfig(minimum_training_rows=200,
                                minimum_class_rows=20)
        rows = [{
            "date": 20231201+i % 20, "label_end": 20231220+i % 10,
            "label": i % 2, "group": "g{}".format(i % 30),
            **{name: float(i+offset)
               for offset, name in enumerate(ENTRY_FEATURES)},
        } for i in range(120)]
        model = YearBoundaryLogisticModel(ENTRY_FEATURES, config).fit(
            pd.DataFrame(rows), (2024,), "group", skip_insufficient=True)
        self.assertNotIn(2024, model.models)
        self.assertEqual(model.manifest[0]["status"],
                         "SKIPPED_INSUFFICIENT_TRAINING_DATA")

    def test_exit_overlay_requires_two_weak_sessions(self):
        config = TimingMLConfig(exit_probability_threshold=.35,
                                exit_persistence_sessions=2)
        model = _ProbabilityModel({int(_Panel.dates[70]): .2,
                                   int(_Panel.dates[71]): .2})
        overlay = ExitContinuationOverlay(model, {}, config)
        self.assertIsNone(overlay.signal(_Panel(), None, 70, "A", _State()))
        self.assertEqual(
            overlay.signal(_Panel(), None, 71, "A", _State()),
            "ML_CONTINUATION_EXIT")

    def test_missing_exit_model_never_exits(self):
        overlay = ExitContinuationOverlay(_ProbabilityModel({}), {})
        self.assertIsNone(overlay.signal(_Panel(), None, 70, "A", _State()))
        self.assertEqual(overlay.decisions[-1]["status"], "MISSING_MODEL")

    def test_entry_tail_label_stops_before_profit(self):
        panel = _Panel()
        panel.open = panel.open.copy()
        panel.low = panel.low.copy()
        panel.high = panel.high.copy()
        day = 60
        panel.close[day, 0] = 10.3
        panel.open[day+1, 0] = 10.0
        panel.low[day+1:day+6, 0] = 10.0
        panel.high[day+1:day+6, 0] = 10.05
        panel.low[day+2, 0] = 8.0
        source = pd.DataFrame([{
            "date": int(panel.dates[day]), "day": day,
            "column": 0, "symbol": "A"}])
        result = add_entry_tail_risk_labels(panel, source, 5, 1.0)
        self.assertEqual(result.label.iloc[0], 1.0)
        self.assertEqual(result.tail_event_offset.iloc[0], 2)

    def test_holding_label_censors_stagnation_exit(self):
        panel = _Panel()
        frame = pd.DataFrame([
            {"date": int(panel.dates[60]), "trade_id": "T", "symbol": "A"}])
        trades = pd.DataFrame([{
            "trade_id": "T", "symbol": "A",
            "opened_at": int(panel.dates[55]),
            "closed_at": int(panel.dates[63])}])
        exits = pd.DataFrame([{
            "date": int(panel.dates[62]), "symbol": "A",
            "reason": "STAGNATION"}])
        result = add_holding_protective_exit_labels(
            panel, frame, trades, exits, horizon_sessions=5)
        self.assertTrue(np.isnan(result.label.iloc[0]))
        self.assertEqual(result.label_event_reason.iloc[0], "STAGNATION")


if __name__ == '__main__':
    unittest.main()

# -*- encoding: utf-8 -*-
"""Adapter exposing the existing Alpha158 feature and label implementation."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ...AlphaBu.ABuAlpha158Lite import (
    ALPHA158_LITE_FEATURES, Alpha158LiteConfig, Alpha158LiteFeatureEngine,
)
from ..ABuMLContracts import FeatureView, LabelContract, parse_asof
from ..ABuMLDataset import MLResearchDataset
from ..ABuMLStrategyAdapter import MLStrategyAdapter


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _market_asof(date_value, minute=0):
    value = datetime.strptime(str(int(date_value)), "%Y%m%d").date()
    return datetime.combine(value, time(15, minute), tzinfo=SHANGHAI).isoformat()


class Alpha158MLAdapter(MLStrategyAdapter):

    def __init__(self, config=None):
        self.config = config or Alpha158LiteConfig()
        self.feature_view = FeatureView(
            self.config.feature_version, tuple(ALPHA158_LITE_FEATURES))
        self.label_contract = LabelContract(
            self.config.label_version, self.config.label_horizon_sessions)

    @property
    def strategy_id(self):
        return self.config.strategy_version

    def build_dataset(self, data_snapshot, signal_days=None,
                      data_available_at=None):
        """Build views by calling the frozen engine, never copying formulas."""
        panel = data_snapshot
        engine = Alpha158LiteFeatureEngine(panel, self.config)
        if signal_days is None:
            signal_days = range(self.config.minimum_history_sessions,
                                len(panel.dates))
        if data_available_at is None:
            data_available_at = _market_asof(panel.dates[-1], minute=5)
        available_cutoff = parse_asof(data_available_at)
        frames = []
        horizon = self.config.label_horizon_sessions
        for day_value in signal_days:
            day = int(day_value)
            snapshot = engine.snapshot(day, include_labels=True)
            if snapshot.empty:
                continue
            signal_asof = _market_asof(panel.dates[day])
            label_index = day + horizon
            if label_index < len(panel.dates):
                label_end = _market_asof(panel.dates[label_index])
                label_available_at = _market_asof(
                    panel.dates[label_index], minute=5)
                label_mature = parse_asof(label_available_at) <= available_cutoff
            else:
                label_end = None
                label_available_at = None
                label_mature = False
            converted = snapshot[["symbol", *ALPHA158_LITE_FEATURES,
                                  "target_rank"]].copy()
            converted.insert(0, "strategy_id", self.strategy_id)
            converted.insert(1, "signal_asof", signal_asof)
            converted["feature_eligible_asof"] = True
            converted["label"] = converted.pop("target_rank")
            converted["label_end"] = label_end
            converted["label_available_at"] = label_available_at
            converted["label_mature"] = bool(label_mature)
            converted["label_valid"] = (
                bool(label_mature) & np.isfinite(converted["label"]))
            converted["query_id"] = str(int(panel.dates[day]))
            frames.append(converted)
        columns = [
            "strategy_id", "signal_asof", "symbol",
            "feature_eligible_asof", "label", "label_end",
            "label_available_at", "label_mature", "label_valid", "query_id",
            *ALPHA158_LITE_FEATURES,
        ]
        frame = (pd.concat(frames, ignore_index=True) if frames
                 else pd.DataFrame(columns=columns))
        return MLResearchDataset(frame, self.feature_view,
                                 self.label_contract)

    def expected_prediction_keys(self, dataset, folds, candidate_arm_id):
        rows = dataset.prediction_rows()
        keys = []
        for fold in folds:
            start = parse_asof(fold.prediction_block_start)
            end = parse_asof(fold.prediction_block_end)
            selected = rows[rows.signal_asof.map(
                lambda value: start <= parse_asof(value) <= end)]
            keys.extend((row.signal_asof, row.symbol, fold.fold_id,
                         candidate_arm_id)
                        for row in selected.itertuples(index=False))
        return tuple(sorted(keys))

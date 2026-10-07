# -*- encoding: utf-8 -*-
"""Dataset views that keep prediction rows separate from mature labels."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .ABuMLContracts import FeatureView, LabelContract, MLContractError, parse_asof


REQUIRED_COLUMNS = {
    "strategy_id", "signal_asof", "symbol", "feature_eligible_asof",
    "label", "label_end", "label_available_at", "label_mature",
    "label_valid", "query_id",
}


class MLResearchDataset(object):
    """Validated research table with explicit train/diagnostic/predict masks."""

    def __init__(self, frame, feature_view, label_contract):
        if not isinstance(feature_view, FeatureView):
            raise TypeError("feature_view must be FeatureView")
        if not isinstance(label_contract, LabelContract):
            raise TypeError("label_contract must be LabelContract")
        missing = (REQUIRED_COLUMNS | set(feature_view.columns)) - set(frame.columns)
        if missing:
            raise MLContractError("dataset missing columns: {}".format(
                ", ".join(sorted(missing))))
        self.feature_view = feature_view
        self.label_contract = label_contract
        self._frame = frame.copy(deep=True).reset_index(drop=True)
        duplicate = self._frame.duplicated(["signal_asof", "symbol"])
        if duplicate.any():
            raise MLContractError("duplicate signal_asof/symbol rows")
        for name in ("feature_eligible_asof", "label_mature", "label_valid"):
            if not self._frame[name].map(lambda value: isinstance(
                    value, (bool, np.bool_))).all():
                raise MLContractError("{} must be boolean".format(name))
        mature = self._frame["label_mature"]
        if self._frame.loc[mature, "label_available_at"].isna().any():
            raise MLContractError("mature labels require label_available_at")
        if self._frame.loc[mature, "label"].isna().any():
            raise MLContractError("mature labels require values")

    @property
    def frame(self):
        return self._frame.copy(deep=True)

    def prediction_rows(self):
        """All as-of eligible rows, regardless of future label availability."""
        return self._frame.loc[self._frame["feature_eligible_asof"]].copy()

    def _available_mask(self, as_of):
        if as_of is None:
            return pd.Series(True, index=self._frame.index)
        cutoff = parse_asof(as_of)

        def available(value):
            return pd.notna(value) and parse_asof(value) <= cutoff
        return self._frame["label_available_at"].map(available)

    def training_rows(self, as_of=None):
        mask = (self._frame["feature_eligible_asof"] &
                self._frame["label_mature"] & self._frame["label_valid"] &
                self._available_mask(as_of))
        return self._frame.loc[mask].copy()

    def validation_rows(self, as_of=None):
        return self.training_rows(as_of)

    def diagnostic_rows(self, as_of=None):
        mask = (self._frame["feature_eligible_asof"] &
                self._frame["label_mature"] & self._frame["label_valid"] &
                self._available_mask(as_of))
        return self._frame.loc[mask].copy()

    def prediction_matrix(self):
        rows = self.prediction_rows()
        return rows.loc[:, list(self.feature_view.columns)].copy()

    @staticmethod
    def date_equal_weights(frame):
        if frame.empty:
            return np.array([], dtype=float)
        counts = frame.groupby("signal_asof")["signal_asof"].transform("size")
        date_count = frame["signal_asof"].nunique()
        weights = len(frame) / (date_count * counts.astype(float))
        return weights.to_numpy(dtype=float)

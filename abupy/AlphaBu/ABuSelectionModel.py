# -*- encoding: utf-8 -*-
"""Small-sample, deterministic models for VCP candidate quality ranking."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuSelectionFeatures import VCP_QUALITY_FEATURES
from .ABuWalkForward import PurgedWalkForward, WalkForwardConfig


@dataclass(frozen=True)
class VCPQualityModelConfig:
    strategy_version: str = "vcp_quality_rank_v1"
    feature_version: str = "vcp_quality_features_v1"
    label_version: str = "vcp_execution_labels_v1"
    ridge_alpha: float = 10.0
    logistic_c: float = 0.10
    false_breakout_penalty_return: float = 0.03
    minimum_train_rows: int = 200
    minimum_class_rows: int = 20

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def load_vcp_quality_model_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(VCPQualityModelConfig)}
    if set(payload) != expected:
        raise ValueError("VCPQualityModelConfig fields mismatch")
    return VCPQualityModelConfig(**payload)


class VCPQualityModel(object):
    """Predict false-breakout probability and a configured return target."""

    def __init__(self, config=None, feature_columns=VCP_QUALITY_FEATURES,
                 return_target="excess_return_20d"):
        self.config = config or VCPQualityModelConfig()
        self.feature_columns = tuple(feature_columns)
        self.return_target = str(return_target)
        self.false_model = None
        self.return_model = None
        self.manifest = None

    @staticmethod
    def _pipeline(estimator):
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", estimator),
        ])

    def fit(self, frame, train_start=None, train_end=None):
        from sklearn.linear_model import LogisticRegression, Ridge

        missing = set(self.feature_columns).difference(frame.columns)
        if missing:
            raise ValueError("missing model features: {}".format(sorted(missing)))
        false_rows = frame.dropna(subset=["false_breakout_5d"])
        if self.return_target not in frame:
            raise ValueError("missing return target: {}".format(self.return_target))
        return_rows = frame.dropna(subset=[self.return_target])
        if len(false_rows) < self.config.minimum_train_rows or \
                len(return_rows) < self.config.minimum_train_rows:
            raise ValueError("insufficient training rows")
        false_target = false_rows.false_breakout_5d.astype(int)
        counts = false_target.value_counts()
        if len(counts) != 2 or counts.min() < self.config.minimum_class_rows:
            raise ValueError("insufficient false-breakout class observations")
        self.false_model = self._pipeline(LogisticRegression(
            C=self.config.logistic_c, class_weight="balanced",
            max_iter=2000, random_state=20261003,
        ))
        self.return_model = self._pipeline(Ridge(alpha=self.config.ridge_alpha))
        self.false_model.fit(false_rows[list(self.feature_columns)], false_target)
        self.return_model.fit(
            return_rows[list(self.feature_columns)],
            return_rows[self.return_target].astype(float),
        )
        coverage = frame[list(self.feature_columns)].notna().mean().to_dict()
        self.manifest = {
            "strategy_version": self.config.strategy_version,
            "model_config_sha256": self.config.sha256,
            "feature_columns": list(self.feature_columns),
            "train_rows_false_breakout": int(len(false_rows)),
            "train_rows_return": int(len(return_rows)),
            "train_signal_dates": int(frame.signal_asof.nunique())
            if "signal_asof" in frame else None,
            "train_start": int(train_start) if train_start is not None else None,
            "train_end": int(train_end) if train_end is not None else None,
            "feature_coverage": {key: float(value)
                                 for key, value in coverage.items()},
            "model_family": "l2_logistic_plus_ridge",
            "return_target": self.return_target,
        }
        return self

    def predict(self, frame):
        if self.false_model is None or self.return_model is None:
            raise ValueError("model is not fitted")
        features = frame[list(self.feature_columns)]
        probability = self.false_model.predict_proba(features)[:, 1]
        expected_return = self.return_model.predict(features)
        score = (expected_return -
                 self.config.false_breakout_penalty_return * probability)
        return pd.DataFrame({
            "false_breakout_probability": probability,
            "predicted_target": expected_return,
            "prediction_target": self.return_target,
            "quality_score": score,
        }, index=frame.index)


def walk_forward_quality_predictions(frame, calendar_dates, model_config=None,
                                     split_config=None,
                                     feature_columns=VCP_QUALITY_FEATURES,
                                     return_target="excess_return_20d"):
    """Return predictions generated only by earlier, purged training data."""
    model_config = model_config or VCPQualityModelConfig()
    split_config = split_config or WalkForwardConfig()
    result = []
    splitter = PurgedWalkForward(split_config)
    for fold in splitter.split(frame.signal_asof.to_numpy(), calendar_dates):
        train = frame.iloc[fold.train_indices]
        if len(train) < model_config.minimum_train_rows:
            continue
        model = VCPQualityModel(
            model_config, feature_columns, return_target=return_target)
        try:
            model.fit(train, fold.train_start, fold.train_end)
        except ValueError as error:
            if str(error) in (
                    "insufficient training rows",
                    "insufficient false-breakout class observations"):
                continue
            raise
        test = frame.iloc[fold.test_indices]
        predicted = model.predict(test)
        predicted.insert(0, "row_index", test.index.to_numpy())
        predicted["fold"] = fold.fold
        predicted["train_start"] = fold.train_start
        predicted["train_end"] = fold.train_end
        predicted["validation_start"] = fold.validation_start
        predicted["validation_end"] = fold.validation_end
        predicted["test_start"] = fold.test_start
        predicted["test_end"] = fold.test_end
        predicted["model_config_sha256"] = model_config.sha256
        result.append(predicted)
    if not result:
        return pd.DataFrame(columns=[
            "row_index", "false_breakout_probability",
            "predicted_target", "prediction_target", "quality_score", "fold",
            "train_start", "train_end", "validation_start", "validation_end",
            "test_start", "test_end", "model_config_sha256",
        ])
    output = pd.concat(result, ignore_index=True)
    if output.row_index.duplicated().any():
        raise AssertionError("walk-forward test rows overlap")
    return output.sort_values("row_index", kind="mergesort").reset_index(drop=True)

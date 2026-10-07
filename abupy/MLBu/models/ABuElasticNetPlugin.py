# -*- encoding: utf-8 -*-
"""Frozen ElasticNet candidate with validation-only parameter selection."""
from __future__ import annotations

import warnings

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet

from ...AlphaBu.ABuAlpha158Lite import ALPHA158_LITE_FEATURES
from ..ABuMLContracts import MLContractError
from ..ABuMLDataset import MLResearchDataset
from ..ABuMLModelPlugin import MLModelPlugin


ALPHAS = (0.00001, 0.0001, 0.001, 0.01)
L1_RATIOS = (0.10, 0.50, 0.90)


class ElasticNetFoldFailure(MLContractError):
    pass


def date_equal_mse(frame, predictions, target_column="target_rank"):
    values = frame[["signal_asof", target_column]].copy()
    values["squared_error"] = (
        np.asarray(predictions, dtype=float) -
        values[target_column].to_numpy(dtype=float)) ** 2
    return float(values.groupby("signal_asof", sort=True)
                 .squared_error.mean().mean())


def select_best_candidate(candidates, tie_tolerance=1e-12):
    valid = [item for item in candidates
             if item.get("converged") and np.isfinite(item.get("mse", np.nan))]
    if not valid:
        raise ElasticNetFoldFailure("all ElasticNet grid candidates failed")
    best_mse = min(item["mse"] for item in valid)
    tied = [item for item in valid
            if item["mse"] <= best_mse + tie_tolerance]
    return max(tied, key=lambda item: (item["alpha"], item["l1_ratio"]))


class ElasticNetPlugin(MLModelPlugin):
    _model_id = "elastic_net_26f_v1"

    def __init__(self, features=ALPHA158_LITE_FEATURES):
        self.features = tuple(features)
        self.imputer = None
        self.model = None
        self.selection = None
        self.grid_results = None

    @property
    def model_id(self):
        return self._model_id

    def fit(self, training, validation):
        required = {"signal_asof", "target_rank", *self.features}
        for name, frame in (("training", training), ("validation", validation)):
            missing = required - set(frame.columns)
            if missing:
                raise MLContractError("{} missing columns: {}".format(
                    name, ", ".join(sorted(missing))))
            if frame.empty or frame.target_rank.isna().any():
                raise MLContractError("{} labels must be mature and valid".format(name))
        self.imputer = SimpleImputer(strategy="median")
        x_train = self.imputer.fit_transform(training[list(self.features)])
        x_valid = self.imputer.transform(validation[list(self.features)])
        y_train = training.target_rank.to_numpy(dtype=float)
        weights = MLResearchDataset.date_equal_weights(training)
        results = []
        fitted = {}
        for alpha in ALPHAS:
            for l1_ratio in L1_RATIOS:
                model = ElasticNet(
                    alpha=alpha, l1_ratio=l1_ratio, fit_intercept=True,
                    positive=False, selection="cyclic", max_iter=20000,
                    tol=1e-6, precompute=True)
                converged = True
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ConvergenceWarning)
                    try:
                        model.fit(x_train, y_train, sample_weight=weights)
                        prediction = model.predict(x_valid)
                    except (FloatingPointError, ValueError):
                        converged = False
                        prediction = np.full(len(validation), np.nan)
                if any(issubclass(item.category, ConvergenceWarning)
                       for item in caught):
                    converged = False
                if not (np.isfinite(model.coef_).all() and
                        np.isfinite(model.intercept_)):
                    converged = False
                mse = (date_equal_mse(validation, prediction)
                       if converged and np.isfinite(prediction).all()
                       else np.nan)
                result = {"alpha": float(alpha),
                          "l1_ratio": float(l1_ratio),
                          "mse": float(mse), "converged": bool(converged)}
                results.append(result)
                if converged:
                    fitted[(float(alpha), float(l1_ratio))] = model
        selected = select_best_candidate(results)
        self.model = fitted[(selected["alpha"], selected["l1_ratio"])]
        self.selection = dict(selected)
        self.grid_results = tuple(results)
        return self

    def predict(self, frame):
        if self.model is None or self.imputer is None:
            raise ValueError("model is not fitted")
        values = self.model.predict(
            self.imputer.transform(frame[list(self.features)]))
        if not np.isfinite(values).all():
            raise ElasticNetFoldFailure("ElasticNet produced non-finite predictions")
        return values

    @property
    def manifest(self):
        if self.model is None:
            raise ValueError("model is not fitted")
        coefficients = self.model.coef_.astype(float)
        return {
            "model_id": self.model_id,
            "features": list(self.features),
            "selected": dict(self.selection),
            "grid_results": [dict(item) for item in self.grid_results],
            "imputer_statistics": self.imputer.statistics_.astype(float).tolist(),
            "intercept": float(self.model.intercept_),
            "coefficients": coefficients.tolist(),
            "nonzero_coefficients": int(np.count_nonzero(coefficients)),
            "max_iter": 20000, "tol": 1e-6, "selection": "cyclic",
            "precompute": True,
        }

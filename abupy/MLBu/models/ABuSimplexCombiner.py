# -*- encoding: utf-8 -*-
"""Frozen non-negative simplex combiner shrunk toward seven-family equality."""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize

from ..ABuMLContracts import MLContractError
from ..ABuMLDataset import MLResearchDataset
from ..ABuMLModelPlugin import MLModelPlugin
from ..adapters.ABuAllMeanRankMLAdapter import (
    AllMeanRankMLAdapter, FAMILY_RANK_COLUMNS,
)


LAMBDAS = (0.01, 0.10, 1.00, 10.00)
OPTIMIZER = "SLSQP"
MAX_ITER = 2000
TOLERANCE = 1e-12


def date_equal_mse(frame, predictions):
    work = frame[["signal_asof", "target_rank"]].copy()
    work["squared_error"] = (
        np.asarray(predictions, dtype=float)-
        work.target_rank.to_numpy(dtype=float))**2
    return float(work.groupby("signal_asof", sort=True)
                 .squared_error.mean().mean())


def fit_simplex_weights(frame, adapter, shrinkage):
    matrix = adapter.feature_matrix(frame, require_labels=True)
    target = frame.target_rank.to_numpy(dtype=float)
    sample_weight = MLResearchDataset.date_equal_weights(frame)
    equal = np.full(matrix.shape[1], 1.0/matrix.shape[1])

    def objective(weights):
        residual = matrix.dot(weights)-target
        return float(np.average(residual**2, weights=sample_weight) +
                     float(shrinkage)*np.sum((weights-equal)**2))

    result = minimize(
        objective, equal, method=OPTIMIZER,
        bounds=[(0.0, 1.0)]*matrix.shape[1],
        constraints=[{"type": "eq", "fun": lambda weights:
                      float(np.sum(weights)-1.0)}],
        options={"maxiter": MAX_ITER, "ftol": TOLERANCE, "disp": False})
    weights = np.asarray(result.x, dtype=float)
    if not result.success or not np.isfinite(weights).all() or \
            np.min(weights) < -1e-10 or abs(weights.sum()-1.0) > 1e-10:
        raise MLContractError("simplex optimization failed: {}".format(
            result.message))
    weights = np.maximum(weights, 0.0)
    weights /= weights.sum()
    return weights, {"success": True, "iterations": int(result.nit),
                     "objective": float(result.fun),
                     "message": str(result.message)}


class EqualWeightCombiner(MLModelPlugin):
    _model_id = "equal_weight_combiner_v1"

    def __init__(self, adapter=None):
        self.adapter = adapter or AllMeanRankMLAdapter()
        self.weights = np.full(len(FAMILY_RANK_COLUMNS),
                               1.0/len(FAMILY_RANK_COLUMNS))

    @property
    def model_id(self):
        return self._model_id

    def fit(self, training=None, validation=None):
        return self

    def predict(self, frame):
        return self.adapter.feature_matrix(frame).dot(self.weights)

    @property
    def manifest(self):
        return {"model_id": self.model_id,
                "features": list(FAMILY_RANK_COLUMNS),
                "weights": self.weights.tolist(), "fitted": False}


class SimplexCombiner(MLModelPlugin):
    _model_id = "simplex_combiner_v1"

    def __init__(self, adapter=None):
        self.adapter = adapter or AllMeanRankMLAdapter()
        self.weights = None
        self.selection = None
        self.grid_results = None

    @property
    def model_id(self):
        return self._model_id

    def fit(self, training, validation):
        self.adapter.validate(training, require_labels=True)
        self.adapter.validate(validation, require_labels=True)
        candidates = []
        fitted = {}
        for shrinkage in LAMBDAS:
            weights, audit = fit_simplex_weights(
                training, self.adapter, shrinkage)
            prediction = self.adapter.feature_matrix(validation).dot(weights)
            row = {"lambda": float(shrinkage),
                   "validation_mse": date_equal_mse(validation, prediction),
                   "weights": weights.tolist(), "optimizer": audit}
            candidates.append(row)
            fitted[float(shrinkage)] = weights
        best_mse = min(row["validation_mse"] for row in candidates)
        tied = [row for row in candidates
                if row["validation_mse"] <= best_mse+1e-12]
        selected = max(tied, key=lambda row: row["lambda"])
        self.weights = fitted[selected["lambda"]]
        self.selection = dict(selected)
        self.grid_results = tuple(candidates)
        return self

    def predict(self, frame):
        if self.weights is None:
            raise ValueError("combiner is not fitted")
        return self.adapter.feature_matrix(frame).dot(self.weights)

    @property
    def manifest(self):
        if self.weights is None:
            raise ValueError("combiner is not fitted")
        return {
            "model_id": self.model_id, "features": list(FAMILY_RANK_COLUMNS),
            "weights": self.weights.tolist(), "selection": self.selection,
            "grid_results": [dict(row) for row in self.grid_results],
            "optimizer": OPTIMIZER, "max_iter": MAX_ITER,
            "tolerance": TOLERANCE, "intercept": False,
            "constraints": "weights>=0,sum(weights)=1",
        }

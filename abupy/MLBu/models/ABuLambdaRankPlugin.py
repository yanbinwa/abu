# -*- encoding: utf-8 -*-
"""Frozen LightGBM LambdaRank plugin with date-equal validation NDCG."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ...AlphaBu.ABuAlpha158Lite import ALPHA158_LITE_FEATURES
from ..ABuMLContracts import (
    MLContractError, assert_model_dependencies, load_model_registry,
)
from ..ABuMLModelPlugin import MLModelPlugin


LABEL_GAIN = np.asarray([0, 1, 2, 3, 4, 5], dtype=float)


def relevance_from_average_rank(average_rank, query_size):
    rank = float(average_rank)
    middle = float(query_size) / 2.0
    if rank > middle:
        return 0
    if rank <= 10:
        return 5
    if rank <= 20:
        return 4
    if rank <= 50:
        return 3
    if rank <= 100:
        return 2
    return 1


def encode_query_relevance(target):
    series = pd.Series(np.asarray(target, dtype=float))
    if series.isna().any() or not len(series):
        raise MLContractError("LambdaRank query labels must be mature")
    ranks = series.rank(method="average", ascending=False)
    encoded = np.asarray([
        relevance_from_average_rank(rank, len(series)) for rank in ranks
    ], dtype=np.int32)
    return encoded


@dataclass(frozen=True)
class RankingMatrix:
    frame: pd.DataFrame
    features: np.ndarray
    labels: np.ndarray
    groups: tuple
    weights: np.ndarray | None
    dropped_queries: tuple


def build_ranking_matrix(frame, features=ALPHA158_LITE_FEATURES,
                         training_weights=False):
    required = {"signal_asof", "symbol", "target_rank", *features}
    missing = required - set(frame.columns)
    if missing:
        raise MLContractError("ranking frame missing: {}".format(
            ", ".join(sorted(missing))))
    ordered = frame.sort_values(
        ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
    kept, labels, groups, dropped = [], [], [], []
    for query_id, group in ordered.groupby("signal_asof", sort=True):
        relevance = encode_query_relevance(group.target_rank)
        if len(np.unique(relevance)) < 2:
            dropped.append(str(query_id))
            continue
        kept.append(group)
        labels.extend(relevance.tolist())
        groups.append(len(group))
    if not kept:
        raise MLContractError("no non-degenerate LambdaRank queries")
    selected = pd.concat(kept, ignore_index=True)
    matrix = selected[list(features)].to_numpy(dtype=float)
    # Median imputation is fitted outside so train statistics can be reused.
    weights = None
    if training_weights:
        total = len(selected)
        query_count = len(groups)
        weights = np.concatenate([
            np.full(size, total/(query_count*size), dtype=float)
            for size in groups])
    return RankingMatrix(
        frame=selected, features=matrix,
        labels=np.asarray(labels, dtype=np.int32), groups=tuple(groups),
        weights=weights, dropped_queries=tuple(dropped))


def _dcg(labels, scores, k):
    order = np.argsort(-np.asarray(scores), kind="mergesort")[:k]
    gains = LABEL_GAIN[np.asarray(labels, dtype=int)[order]]
    discounts = np.log2(np.arange(len(order), dtype=float)+2.0)
    return float(np.sum(gains/discounts))


def ndcg_at_k(labels, scores, k):
    ideal = _dcg(labels, LABEL_GAIN[np.asarray(labels, dtype=int)], k)
    return 1.0 if ideal <= 0 else _dcg(labels, scores, k)/ideal


def date_equal_ndcg(labels, scores, groups, k):
    values, offset = [], 0
    for size in groups:
        values.append(ndcg_at_k(labels[offset:offset+size],
                                scores[offset:offset+size], k))
        offset += size
    if offset != len(labels):
        raise MLContractError("query groups do not cover labels")
    return float(np.mean(values))


class LambdaRankPlugin(MLModelPlugin):
    _model_id = "lambdarank_26f_v1"

    def __init__(self, features=ALPHA158_LITE_FEATURES):
        self.features = tuple(features)
        self.imputer = None
        self.booster = None
        self.best_iteration = None
        self.evaluation = None
        self.query_audit = None

    @property
    def model_id(self):
        return self._model_id

    def fit(self, training, validation):
        assert_model_dependencies([self.model_id], load_model_registry())
        try:
            import lightgbm as lgb
        except OSError as exc:
            if "libomp.dylib" not in str(exc):
                raise
            raise MLContractError(
                "LightGBM requires libomp before Python starts; on this "
                "workspace use DYLD_LIBRARY_PATH=$(brew --prefix "
                "libomp)/lib") from exc
        from sklearn.impute import SimpleImputer

        train = build_ranking_matrix(
            training, self.features, training_weights=True)
        valid = build_ranking_matrix(
            validation, self.features, training_weights=False)
        self.imputer = SimpleImputer(strategy="median")
        x_train = self.imputer.fit_transform(train.features)
        x_valid = self.imputer.transform(valid.features)
        train_set = lgb.Dataset(
            x_train, label=train.labels, group=list(train.groups),
            weight=train.weights, feature_name=list(self.features),
            free_raw_data=False)
        # Deliberately omit validation weights. Native NDCG then averages
        # query metrics equally instead of weighting dates by 1 / query size.
        valid_set = lgb.Dataset(
            x_valid, label=valid.labels, group=list(valid.groups),
            reference=train_set, feature_name=list(self.features),
            free_raw_data=False)
        params = {
            "objective": "lambdarank", "metric": "ndcg",
            "ndcg_eval_at": [10, 20, 50],
            "lambdarank_truncation_level": 20,
            "lambdarank_norm": True,
            "label_gain": [0, 1, 2, 3, 4, 5],
            "learning_rate": 0.03, "num_leaves": 7, "max_depth": 3,
            "min_data_in_leaf": 500, "lambda_l1": 1.0,
            "lambda_l2": 10.0, "feature_fraction": 1.0,
            "bagging_fraction": 1.0, "bagging_freq": 0, "max_bin": 63,
            "deterministic": True, "force_col_wise": True,
            "seed": 20261007, "num_threads": 1, "verbosity": -1,
        }
        history = {}
        self.booster = lgb.train(
            params, train_set, num_boost_round=300,
            valid_sets=[valid_set], valid_names=["validation"],
            callbacks=[
                lgb.early_stopping(30, first_metric_only=True, verbose=False),
                lgb.record_evaluation(history),
            ])
        self.best_iteration = int(self.booster.best_iteration)
        prediction = self.booster.predict(
            x_valid, num_iteration=self.best_iteration)
        manual = date_equal_ndcg(
            valid.labels, prediction, valid.groups, 10)
        native = float(self.booster.best_score["validation"]["ndcg@10"])
        if abs(manual-native) > 1e-12:
            raise MLContractError(
                "native validation NDCG is not date-equal: {} vs {}".format(
                    native, manual))
        self.evaluation = {
            "native_ndcg_at_10": native,
            "manual_date_equal_ndcg_at_10": manual,
            "history": history,
        }
        self.query_audit = {
            "train_groups": list(train.groups),
            "validation_groups": list(valid.groups),
            "train_weight_sums": [float(train.weights[
                sum(train.groups[:index]):sum(train.groups[:index+1])].sum())
                for index in range(len(train.groups))],
            "validation_weights": None,
            "dropped_train_queries": list(train.dropped_queries),
            "dropped_validation_queries": list(valid.dropped_queries),
        }
        return self

    def predict(self, frame):
        if self.booster is None:
            raise ValueError("model is not fitted")
        matrix = self.imputer.transform(frame[list(self.features)])
        return self.booster.predict(matrix, num_iteration=self.best_iteration)

    @property
    def manifest(self):
        if self.booster is None:
            raise ValueError("model is not fitted")
        return {
            "model_id": self.model_id,
            "features": list(self.features),
            "best_iteration": self.best_iteration,
            "evaluation": self.evaluation,
            "query_audit": self.query_audit,
            "imputer_statistics": self.imputer.statistics_.astype(float).tolist(),
            "model_text": self.booster.model_to_string(
                num_iteration=self.best_iteration),
        }

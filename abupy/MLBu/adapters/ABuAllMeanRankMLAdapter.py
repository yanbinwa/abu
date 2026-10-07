# -*- encoding: utf-8 -*-
"""Strict seven-family OOS score view for A0/A1 combiner research."""
from __future__ import annotations

import numpy as np

from ..ABuMLContracts import MLContractError
from ..ABuMLFamilyOOSAudit import FAMILIES


FAMILY_RANK_COLUMNS = tuple(
    "family_{}_rank".format(family) for family in FAMILIES)
SOURCE_MANIFEST_COLUMNS = tuple(
    "family_{}_source_manifest_id".format(family) for family in FAMILIES)
SOURCE_FOLD_COLUMNS = tuple(
    "family_{}_fold".format(family) for family in FAMILIES)


class AllMeanRankMLAdapter(object):
    """Expose only family OOS ranks; raw/financial factors are inaccessible."""

    feature_columns = FAMILY_RANK_COLUMNS

    def validate(self, frame, require_labels=False):
        required = {"signal_asof", "symbol", *FAMILY_RANK_COLUMNS,
                    *SOURCE_MANIFEST_COLUMNS, *SOURCE_FOLD_COLUMNS}
        if require_labels:
            required.add("target_rank")
        missing = required-set(frame.columns)
        if missing:
            raise MLContractError("seven-family view missing: {}".format(
                ", ".join(sorted(missing))))
        if frame.duplicated(["signal_asof", "symbol"]).any():
            raise MLContractError("seven-family view has duplicate keys")
        if frame[list(FAMILY_RANK_COLUMNS)].isna().any().any():
            raise MLContractError("seven-family ranks must be complete")
        for column in SOURCE_MANIFEST_COLUMNS:
            if frame[column].isna().any() or \
                    frame[column].astype(str).str.len().eq(0).any():
                raise MLContractError("missing OOS source manifest: " + column)
        for family, manifest_column, fold_column in zip(
                FAMILIES, SOURCE_MANIFEST_COLUMNS, SOURCE_FOLD_COLUMNS):
            counts = frame.groupby("signal_asof", sort=False)[
                [manifest_column, fold_column]].nunique(dropna=False)
            if (counts > 1).any().any():
                raise MLContractError(
                    "family source/fold varies within date: " + family)
        if require_labels and frame.target_rank.isna().any():
            raise MLContractError("combiner fit labels must be mature")
        return True

    def feature_matrix(self, frame, require_labels=False):
        self.validate(frame, require_labels=require_labels)
        values = frame.loc[:, FAMILY_RANK_COLUMNS].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise MLContractError("seven-family matrix is non-finite")
        return values

    def equal_weight_score(self, frame):
        return self.feature_matrix(frame).mean(axis=1)


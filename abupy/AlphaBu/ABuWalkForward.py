# -*- encoding: utf-8 -*-
"""Purged expanding walk-forward splits for overlapping return labels."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class WalkForwardConfig:
    minimum_train_dates: int = 60
    validation_dates: int = 20
    test_dates: int = 20
    label_horizon_sessions: int = 20
    embargo_sessions: int = 0

    def __post_init__(self):
        for name in ("minimum_train_dates", "validation_dates", "test_dates",
                     "label_horizon_sessions"):
            if getattr(self, name) <= 0:
                raise ValueError("{} must be positive".format(name))
        if self.embargo_sessions < 0:
            raise ValueError("embargo_sessions cannot be negative")


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_indices: np.ndarray
    validation_indices: np.ndarray
    test_indices: np.ndarray
    train_start: int
    train_end: int
    validation_start: int
    validation_end: int
    test_start: int
    test_end: int


class PurgedWalkForward(object):

    def __init__(self, config=None):
        self.config = config or WalkForwardConfig()

    def split(self, sample_dates, calendar_dates):
        sample_dates = np.asarray(sample_dates, dtype=np.int64)
        calendar_dates = np.asarray(calendar_dates, dtype=np.int64)
        if len(calendar_dates) != len(np.unique(calendar_dates)) or \
                np.any(np.diff(calendar_dates) <= 0):
            raise ValueError("calendar_dates must be unique and increasing")
        calendar_position = {int(value): index
                             for index, value in enumerate(calendar_dates)}
        if any(int(value) not in calendar_position for value in sample_dates):
            raise ValueError("every sample date must exist in calendar_dates")
        unique = np.array(sorted(set(int(value) for value in sample_dates)),
                          dtype=np.int64)
        cfg = self.config
        initial = cfg.minimum_train_dates + cfg.validation_dates
        fold_number = 0
        for test_offset in range(initial, len(unique), cfg.test_dates):
            test_dates = unique[test_offset:test_offset+cfg.test_dates]
            if not len(test_dates):
                continue
            test_start_position = calendar_position[int(test_dates[0])]
            validation_cutoff = (test_start_position -
                                 cfg.label_horizon_sessions -
                                 cfg.embargo_sessions)
            validation_candidates = np.array([
                value for value in unique[:test_offset]
                if calendar_position[int(value)] <= validation_cutoff
            ], dtype=np.int64)
            if len(validation_candidates) < cfg.validation_dates:
                continue
            validation_dates = validation_candidates[-cfg.validation_dates:]
            validation_start_position = calendar_position[
                int(validation_dates[0])]
            train_cutoff = (validation_start_position -
                            cfg.label_horizon_sessions -
                            cfg.embargo_sessions)
            train_dates = np.array([
                value for value in unique
                if calendar_position[int(value)] <= train_cutoff
            ], dtype=np.int64)
            if len(train_dates) < cfg.minimum_train_dates:
                continue
            train_indices = np.flatnonzero(np.isin(sample_dates, train_dates))
            validation_indices = np.flatnonzero(
                np.isin(sample_dates, validation_dates))
            test_indices = np.flatnonzero(np.isin(sample_dates, test_dates))
            if not (len(train_indices) and len(validation_indices) and
                    len(test_indices)):
                continue
            yield WalkForwardFold(
                fold=fold_number,
                train_indices=train_indices,
                validation_indices=validation_indices,
                test_indices=test_indices,
                train_start=int(train_dates[0]), train_end=int(train_dates[-1]),
                validation_start=int(validation_dates[0]),
                validation_end=int(validation_dates[-1]),
                test_start=int(test_dates[0]), test_end=int(test_dates[-1]),
            )
            fold_number += 1


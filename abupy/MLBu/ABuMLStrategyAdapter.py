# -*- encoding: utf-8 -*-
"""Interface for adapting a frozen strategy to the ML research pipeline."""
from __future__ import annotations

from abc import ABCMeta, abstractmethod


class MLStrategyAdapter(object, metaclass=ABCMeta):

    @property
    @abstractmethod
    def strategy_id(self):
        raise NotImplementedError

    @abstractmethod
    def build_dataset(self, data_snapshot):
        raise NotImplementedError

    @abstractmethod
    def expected_prediction_keys(self, dataset, folds, candidate_arm_id):
        raise NotImplementedError

# -*- encoding: utf-8 -*-
"""Small interface shared by auditable ML model plugins."""
from __future__ import annotations

from abc import ABCMeta, abstractmethod


class MLModelPlugin(object, metaclass=ABCMeta):

    @property
    @abstractmethod
    def model_id(self):
        raise NotImplementedError

    @abstractmethod
    def predict(self, frame):
        raise NotImplementedError

    @property
    @abstractmethod
    def manifest(self):
        raise NotImplementedError

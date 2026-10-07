# -*- encoding: utf-8 -*-
"""Parity wrapper around the existing frozen Alpha158 Ridge model."""
from __future__ import annotations

from ...AlphaBu.ABuAlpha158Lite import Alpha158LiteConfig, Alpha158LiteModel
from ..ABuMLModelPlugin import MLModelPlugin


class RidgePlugin(MLModelPlugin):
    _model_id = "ridge_26f_v1"

    @property
    def model_id(self):
        return self._model_id

    def __init__(self, config=None):
        self.config = config or Alpha158LiteConfig()
        if self.config.ridge_alpha != 100.0:
            raise ValueError("ridge_26f_v1 requires alpha=100")
        self.model = Alpha158LiteModel(self.config)

    def fit(self, frame, train_dates):
        self.model.fit(frame, train_dates)
        return self

    def predict(self, frame):
        return self.model.predict(frame)

    @property
    def manifest(self):
        if self.model.manifest is None:
            raise ValueError("model is not fitted")
        return dict(self.model.manifest)

# -*- encoding: utf-8 -*-
"""Point-in-time conviction signal used by selective sizing experiments."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ConvictionSizingConfig:
    policy_id: str = "alpha158_conviction_sizing_v1"
    market_volatility_window: int = 60
    minimum_market_volatility: float = 0.1416509953379142
    maximum_industry_breadth_rank: float = -0.26127973
    normal_risk_fraction: float = 0.0025
    enhanced_risk_fraction: float = 0.003125
    minimum_industry_history: int = 120

    def __post_init__(self):
        if not self.policy_id:
            raise ValueError("policy_id is required")
        if self.market_volatility_window < 20 or \
                self.minimum_industry_history < 20:
            raise ValueError("conviction windows are too short")
        if not 0 < self.normal_risk_fraction <= self.enhanced_risk_fraction <= 1:
            raise ValueError("invalid conviction risk fractions")
        if not -0.5 <= self.maximum_industry_breadth_rank <= 0.5:
            raise ValueError("industry breadth rank must be centered percentile")
        if self.minimum_market_volatility <= 0:
            raise ValueError("market volatility threshold must be positive")

    @property
    def sha256(self):
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_conviction_sizing_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(ConvictionSizingConfig)}
    if set(payload) != expected:
        raise ValueError("ConvictionSizingConfig fields mismatch")
    return ConvictionSizingConfig(**payload)


class ConvictionSizingPolicy(object):
    """Assign a frozen risk fraction from close-t information only."""

    def __init__(self, panel, feature_engine, config=None):
        self.panel = panel
        self.feature_engine = feature_engine
        self.config = config or ConvictionSizingConfig()
        self._day_cache = {}
        self.evaluations = []

    def _market_volatility(self, day):
        window = self.config.market_volatility_window
        if day-window < 0:
            return np.nan
        # Use the same pandas rolling implementation that froze the threshold.
        # The extra close supplies the first return; all inputs end at close t.
        close = pd.Series(np.asarray(
            self.panel.benchmark_close[day-window:day+1], dtype=float))
        value = close.pct_change().rolling(window).std().iloc[-1]
        return float(value*np.sqrt(252)) if np.isfinite(value) else np.nan

    def _industry_breadth_ranks(self, day):
        eligible = self.feature_engine.eligible(day)
        denominator = self.panel.breadth_denominator(
            min_history=self.config.minimum_industry_history,
            unknown_st_policy=self.feature_engine.config.unknown_st_policy)[day]
        industries = np.asarray(self.panel.industry[day], dtype=int)
        raw = np.full(len(self.panel.symbols), np.nan, dtype=float)
        valid_industries = np.unique(
            industries[denominator & (industries >= 0)])
        for industry in valid_industries:
            members = denominator & (industries == industry)
            valid = members & np.isfinite(self.panel.ma60[day]) & \
                np.isfinite(self.panel.close[day])
            if valid.any():
                raw[members] = float(np.mean(
                    self.panel.close[day, valid] > self.panel.ma60[day, valid]))
        columns = np.flatnonzero(eligible & np.isfinite(raw))
        ranks = np.full(len(self.panel.symbols), np.nan, dtype=float)
        if len(columns):
            # Match Alpha158LiteFeatureEngine's frozen float32 rank exactly,
            # including boundary comparisons at the registered threshold.
            ranked = pd.Series(raw[columns].astype(np.float32)).rank(
                method="average", pct=True).to_numpy(dtype=np.float32)
            ranks[columns] = (ranked-np.float32(0.5)).astype(np.float32)
        return ranks

    def _snapshot(self, day):
        cached = self._day_cache.get(int(day))
        if cached is None:
            cached = {
                "market_volatility": self._market_volatility(day),
                "industry_breadth_ranks": self._industry_breadth_ranks(day),
            }
            self._day_cache[int(day)] = cached
        return cached

    def evaluate(self, day, symbol):
        snapshot = self._snapshot(day)
        column = self.panel.symbol_index[str(symbol)]
        market_volatility = float(snapshot["market_volatility"])
        breadth_rank = float(snapshot["industry_breadth_ranks"][column])
        missing = []
        if not np.isfinite(market_volatility):
            missing.append("MARKET_VOLATILITY")
        if not np.isfinite(breadth_rank):
            missing.append("INDUSTRY_BREADTH_RANK")
        selected = (
            not missing and
            market_volatility >=
            self.config.minimum_market_volatility-1e-12 and
            breadth_rank <=
            self.config.maximum_industry_breadth_rank+1e-7)
        risk_fraction = (self.config.enhanced_risk_fraction if selected else
                         self.config.normal_risk_fraction)
        record = {
            "policy_id": self.config.policy_id,
            "config_sha256": self.config.sha256,
            "signal_asof": int(self.panel.dates[day]),
            "symbol": str(symbol),
            "market_volatility_60d": market_volatility,
            "industry_breadth_ma60_rank": breadth_rank,
            "selected": bool(selected),
            "risk_fraction": float(risk_fraction),
            "reason_codes": tuple(missing) if missing else (
                ("HIGH_VOLATILITY", "WEAK_INDUSTRY_BREADTH") if selected else
                ("NORMAL_CONVICTION",)),
        }
        self.evaluations.append(record)
        return record

# -*- encoding: utf-8 -*-
"""One-session VCP follow-through confirmation without future execution data."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import numpy as np

from .ABuTradeIntent import make_record_id


@dataclass(frozen=True)
class VCPFollowThroughConfig:
    strategy_version: str = "vcp_followthrough_v1"
    minimum_close_above_breakout_fraction: float = 0.0
    require_close_above_initial_stop: bool = True
    max_gap_atr: float = 1.0
    max_planned_risk_fraction: float = 0.08

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def load_vcp_followthrough_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(VCPFollowThroughConfig)}
    if set(payload) != expected:
        raise ValueError("VCPFollowThroughConfig fields mismatch")
    return VCPFollowThroughConfig(**payload)


def confirm_followthrough_intents(panel, intents, config=None):
    """Confirm at t+1 close and create an order intent for t+2 open.

    The original breakout level and adjusted stop remain frozen.  Quantity and
    execution are still decided by the common risk engine on the following
    session.  No t+2 price is inspected here.
    """
    config = config or VCPFollowThroughConfig()
    date_index = {int(value): position for position, value in enumerate(panel.dates)}
    confirmed = []
    for source in intents:
        day = date_index.get(int(source.signal_asof))
        column = panel.symbol_index.get(source.symbol)
        if day is None or column is None or day + 2 >= len(panel.dates):
            continue
        confirmation_day = day + 1
        adjusted = float(panel.close[confirmation_day, column])
        raw = float(panel.exec_close[confirmation_day, column])
        atr = float(panel.atr21[confirmation_day, column])
        breakout = float(source.metadata.get("breakout_level", np.nan))
        stop_adjusted = float(source.initial_stop_adjusted)
        if not (np.isfinite(adjusted) and adjusted > 0 and
                np.isfinite(raw) and raw > 0 and np.isfinite(atr) and atr > 0 and
                np.isfinite(breakout) and np.isfinite(stop_adjusted)):
            continue
        threshold = breakout * (
            1 + config.minimum_close_above_breakout_fraction)
        if adjusted <= threshold:
            continue
        if config.require_close_above_initial_stop and adjusted <= stop_adjusted:
            continue
        factor = raw / adjusted
        stop_raw = stop_adjusted * factor
        max_price = raw + config.max_gap_atr * atr * factor
        if (max_price-stop_raw >
                raw*config.max_planned_risk_fraction):
            continue
        metadata = dict(source.metadata)
        metadata.update({
            "source_intent_id": source.intent_id,
            "source_strategy_id": source.strategy_id,
            "original_signal_asof": int(source.signal_asof),
            "confirmation_date": int(panel.dates[confirmation_day]),
            "confirmation_close_adjusted": adjusted,
            "confirmation_distance_atr": (adjusted-breakout)/atr,
            "max_buy_price_raw": max_price,
            "followthrough_version": config.strategy_version,
        })
        confirmed.append(replace(
            source,
            intent_id=make_record_id(
                config.strategy_version, int(panel.dates[confirmation_day]),
                source.symbol, source.intent_id),
            strategy_id=config.strategy_version,
            strategy_version="1",
            signal_asof=int(panel.dates[confirmation_day]),
            signal_price_adjusted=adjusted,
            signal_price_raw=raw,
            adjustment_factor_signal=factor,
            initial_stop_raw=stop_raw,
            max_gap_atr=config.max_gap_atr,
            metadata=metadata,
        ))
    return sorted(confirmed, key=lambda item: (
        item.signal_asof, -item.score, item.symbol))


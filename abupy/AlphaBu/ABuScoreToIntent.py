# -*- encoding: utf-8 -*-
"""Convert external selection scores into immutable trade intents."""
from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

import numpy as np

from .ABuTradeIntent import TradeIntent, make_record_id


def rerank_intents(intents: Sequence[TradeIntent], scores: Mapping[str, float],
                   strategy_id: str, strategy_version: str = "1"):
    """Return newly versioned intents ordered by an external score.

    ``scores`` is keyed by the source intent id.  Missing and non-finite scores
    are rejected explicitly instead of silently falling back to the old score.
    The price, stop, adjustment factor and execution metadata remain frozen.
    """
    ranked = []
    for source in intents:
        value = scores.get(source.intent_id)
        if value is None or not np.isfinite(float(value)):
            continue
        score = float(value)
        metadata = dict(source.metadata)
        metadata.update({
            "source_intent_id": source.intent_id,
            "source_strategy_id": source.strategy_id,
            "source_score": float(source.score),
            "selection_score": score,
        })
        ranked.append(replace(
            source,
            intent_id=make_record_id(
                strategy_id, strategy_version, source.signal_asof, source.symbol),
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            score=score,
            metadata=metadata,
        ))
    return sorted(ranked, key=lambda item: (-item.score, item.symbol))


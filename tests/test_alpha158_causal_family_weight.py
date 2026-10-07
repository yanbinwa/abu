import pandas as pd

from scripts.validate_alpha158_causal_family_weight_v1 import (
    causal_weight_history,
)


def test_weights_use_only_embargoed_rank_ic_and_sum_to_one():
    dates = list(range(1, 11))
    rows = []
    for date in dates:
        rows += [
            {"signal_asof": date, "family": "a", "rank_ic": float(date)},
            {"signal_asof": date, "family": "b", "rank_ic": 1.0},
        ]
    config = {
        "families": ["a", "b"], "label_embargo_sessions": 2,
        "weight_lookback_sessions": 3, "minimum_weight_history_sessions": 2,
        "negative_family_weight": 0.0, "uniform_shrinkage": 0.5,
    }
    weights, available = causal_weight_history(pd.DataFrame(rows), config)
    assert weights.loc[1].to_dict() == {"a": 0.5, "b": 0.5}
    assert weights.loc[3].to_dict() == {"a": 0.5, "b": 0.5}
    assert available.loc[4, "a"] == 1.5
    assert available.loc[4, "b"] == 1.0
    assert weights.loc[4, "a"] > weights.loc[4, "b"]
    assert weights.sum(axis=1).round(12).eq(1.0).all()


def test_current_and_future_ic_cannot_change_current_weight():
    rows = []
    for date in range(1, 9):
        rows += [
            {"signal_asof": date, "family": "a", "rank_ic": 1.0},
            {"signal_asof": date, "family": "b", "rank_ic": 1.0},
        ]
    config = {
        "families": ["a", "b"], "label_embargo_sessions": 2,
        "weight_lookback_sessions": 3, "minimum_weight_history_sessions": 2,
        "negative_family_weight": 0.0, "uniform_shrinkage": 0.5,
    }
    original, _ = causal_weight_history(pd.DataFrame(rows), config)
    changed = pd.DataFrame(rows)
    changed.loc[(changed.signal_asof >= 5) & changed.family.eq("a"), "rank_ic"] = 999
    revised, _ = causal_weight_history(changed, config)
    pd.testing.assert_series_equal(original.loc[6], revised.loc[6])

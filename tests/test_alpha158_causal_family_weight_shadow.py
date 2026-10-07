import json

import pandas as pd
import pytest

from scripts.run_alpha158_causal_family_weight_shadow_v1 import (
    combine_family_scores, load_config, weights_asof,
)


def config():
    value = load_config()
    value["families"] = ["a", "b"]
    value["label_embargo_sessions"] = 2
    value["weight_lookback_sessions"] = 3
    value["minimum_weight_history_sessions"] = 2
    return value


def test_weight_asof_excludes_latest_embargo_sessions():
    history = pd.DataFrame([
        {"signal_asof": day, "family": family,
         "rank_ic": float(day if family == "a" else 1)}
        for day in range(1, 9) for family in ("a", "b")
    ])
    weights, eligible, observations = weights_asof(
        history, list(range(1, 9)), 5, config())
    assert eligible == 4
    assert observations == 3
    assert weights["a"] > weights["b"]
    changed = history.copy()
    changed.loc[changed.signal_asof >= 5, "rank_ic"] = 999
    revised, _, _ = weights_asof(changed, list(range(1, 9)), 5, config())
    pd.testing.assert_series_equal(weights, revised)


def test_model_parity_tolerance_is_frozen_below_score_precision():
    assert load_config()["family_model_parity_absolute_tolerance"] == 1e-8


def test_score_combination_keeps_all_families_and_is_deterministic():
    frames = {
        "a": pd.DataFrame({
            "signal_asof": [1, 1], "symbol": ["x", "y"],
            "column": [0, 1], "candidate_score": [2.0, 1.0]}),
        "b": pd.DataFrame({
            "signal_asof": [1, 1], "symbol": ["x", "y"],
            "column": [0, 1], "candidate_score": [0.0, 3.0]}),
    }
    combined, detail = combine_family_scores(
        frames, pd.Series({"a": .75, "b": .25}))
    assert combined.symbol.tolist() == ["x", "y"]
    assert combined.daily_rank.tolist() == [1, 2]
    assert set(detail.family) == {"a", "b"}


@pytest.mark.parametrize("field", [
    "real_orders_allowed", "broker_connected", "automatic_admission",
    "historical_orders_backfilled", "missed_session_backfill",
])
def test_live_or_backfill_flags_fail_closed(tmp_path, field):
    value = load_config()
    value[field] = True
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="isolation contract"):
        load_config(path)

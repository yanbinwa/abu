#!/usr/bin/env python3
"""Executable ablation for frozen Alpha158 conviction-based sizing."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    Alpha158LiteFeatureEngine, load_alpha158_lite_config,
    load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuConvictionSizing import (  # noqa: E402
    ConvictionSizingPolicy, load_conviction_sizing_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuScaleOutPolicy import ScaleOutConfig  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.analyze_scale_out_experiment import (  # noqa: E402
    _paired_block_bootstrap, _year_returns,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    rank_frame, run_low_turnover,
)
from scripts.backtest_position_add_v1 import (  # noqa: E402
    _metrics, _policy, _write_audit,
)


def _run_variant(name, panel, scores, source, low, base_risk,
                 sizing_config, output, single_risk, portfolio_risk,
                 industry_risk, uniform=False):
    risk = replace(
        base_risk, single_trade_risk_fraction=float(single_risk),
        portfolio_open_risk_fraction=float(portfolio_risk),
        industry_open_risk_fraction=float(industry_risk))
    sizing = None
    if not uniform and sizing_config is not None:
        config = replace(
            sizing_config, policy_id=name,
            enhanced_risk_fraction=float(single_risk))
        sizing = ConvictionSizingPolicy(
            panel, Alpha158LiteFeatureEngine(panel, source), config)
    result, audit = run_low_turnover(
        panel, scores, source, low, risk, 20260930,
        sync_dynamic_stops=True,
        position_add_policy=_policy("protected_winner"),
        position_add_execution_mode="executable",
        scale_out_config=ScaleOutConfig(
            policy_id="scale_out_2r_50_v1", trigger_r_multiples=(2.0,),
            cumulative_exit_fractions=(0.50,)),
        entry_sizing_policy=sizing)
    result.update(_metrics(audit["curve"], audit["fills"]))
    result.update({
        "variant": name,
        "single_trade_risk_ceiling": float(single_risk),
        "normal_trade_risk_fraction": (
            float(single_risk) if uniform or sizing is None else
            float(sizing.config.normal_risk_fraction)),
        "portfolio_open_risk_fraction": float(portfolio_risk),
        "industry_open_risk_fraction": float(industry_risk),
    })
    _write_audit(output, result, audit["curve"], audit["fills"], audit)
    _year_returns(output / "daily_nav.csv").to_csv(
        output / "annual_returns.csv", index=False)
    capital_error = float(np.max(np.abs(
        audit["curve"].capital-audit["curve"].cash-audit["curve"].stocks)))
    if capital_error > 1e-6 or (audit["curve"].cash < -1e-6).any():
        raise AssertionError("capital conservation failed for {}".format(name))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/oos_predictions.csv.gz"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_conviction_sizing_v1_20261004"))
    parser.add_argument("--bootstrap-paths", type=int, default=5000)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("refusing to overwrite {}".format(args.output_dir))
    args.output_dir.mkdir(parents=True)

    source = load_alpha158_lite_config(
        ROOT / "configs/selection/alpha158_lite_v1.json")
    low = load_alpha158_lite_low_turnover_config(
        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    base_risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    sizing = load_conviction_sizing_config(
        ROOT / "configs/selection/alpha158_conviction_sizing_v1.json")
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", low.score_column],
        dtype={"symbol": str})
    scores = rank_frame(
        predictions, low.score_column,
        max(low.entry_rank_limit, low.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20210101, end_date=20261231)

    specifications = [
        ("a0_current", None, .0025, .0200, .0060, False),
        ("a1_selected_03125", sizing, .003125, .0200, .0060, False),
        ("a2_selected_0375", sizing, .00375, .0200, .0060, False),
        ("a3_uniform_03125", None, .003125, .0200, .0060, True),
        ("a4_selected_0375_risk25", sizing, .00375, .0250, .0075, False),
    ]
    results = []
    for spec in specifications:
        name, sizing_config, single_risk, portfolio_risk, industry_risk, \
            uniform = spec
        result = _run_variant(
            name=name, panel=panel, scores=scores, source=source, low=low,
            base_risk=base_risk, sizing_config=sizing_config,
            output=args.output_dir / name, single_risk=single_risk,
            portfolio_risk=portfolio_risk, industry_risk=industry_risk,
            uniform=uniform)
        results.append(result)
        print(name, result["return_pct"], result["max_drawdown_pct"],
              result["average_exposure_pct"], flush=True)

    # The no-op variant must reproduce the already reviewed executable path.
    reference = Path(
        "/Users/wjy/abu/backtests/position_add_v1_scale_out_2r_50_20261004/alpha158/daily_nav.csv")
    expected = pd.read_csv(reference)
    observed = pd.read_csv(args.output_dir / "a0_current/daily_nav.csv")
    for column in ("cash", "stocks", "capital", "exposure", "holdings"):
        if not np.allclose(expected[column], observed[column], rtol=0, atol=1e-8):
            raise AssertionError("A0 golden mismatch in {}".format(column))

    result_frame = pd.DataFrame(results)
    result_frame.to_csv(args.output_dir / "comparison.csv", index=False)
    base = result_frame[result_frame.variant.eq("a0_current")].iloc[0]
    deltas = []
    for row in result_frame[~result_frame.variant.eq("a0_current")].itertuples():
        bootstrap = _paired_block_bootstrap(
            args.output_dir / "a0_current/daily_nav.csv",
            args.output_dir / row.variant / "daily_nav.csv",
            paths=args.bootstrap_paths)
        deltas.append({
            "variant": row.variant,
            "delta_return_pct": row.return_pct-base.return_pct,
            "delta_max_drawdown_pct": row.max_drawdown_pct-base.max_drawdown_pct,
            "delta_es95_pct": (
                row.daily_expected_shortfall_95_pct-
                base.daily_expected_shortfall_95_pct),
            "delta_average_exposure_pct": (
                row.average_exposure_pct-base.average_exposure_pct),
            "delta_risk_rejected": row.risk_rejected-base.risk_rejected,
            "delta_risk_reduced": row.risk_reduced-base.risk_reduced,
            **bootstrap,
        })
    delta_frame = pd.DataFrame(deltas)
    delta_frame.to_csv(args.output_dir / "deltas.csv", index=False)
    manifest = {
        "status": "HISTORICAL_DIAGNOSTIC_ONLY",
        "frozen_signal_config_sha256": sizing.sha256,
        "variants": [item[0] for item in specifications],
        "a0_golden_reproduced": True,
        "bootstrap_paths": int(args.bootstrap_paths),
        "selection_rule_was_observed_before_executable_ablation": True,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    print("\nDeltas\n" + delta_frame.to_string(index=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Backtest the frozen one-session VCP follow-through experiment."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from abupy.AlphaBu.ABuVCPFollowThrough import (  # noqa: E402
    confirm_followthrough_intents, load_vcp_followthrough_config,
)
from scripts.backtest_vcp_context_v1 import (  # noqa: E402
    load_frozen_intents, run_experiment,
)
from scripts.backtest_vcp_quality_rank_v1 import _save_audit  # noqa: E402


def _register(path, trial_id, hypothesis, configuration, status="REGISTERED",
              observed_metrics=None):
    existing = {item["trial_id"]: item for item in read_trial_registry(path)}
    if trial_id in existing:
        return existing[trial_id]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-intents", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_followthrough_v1"))
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/vcp_followthrough_v1.json")
    parser.add_argument("--start-date", type=int, default=20220104)
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = load_vcp_followthrough_config(args.config)
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    registry = args.output_dir / "trial_registry.jsonl"
    trial_id = "vcp-followthrough-v1-frozen-20261003"
    registration = _register(
        registry, trial_id,
        "A single close above the frozen breakout level reduces immediate "
        "breakout failures enough to improve cost-after realized R.",
        {"followthrough": asdict(config), "source_strategy": "vcp_residual_v2",
         "risk_config_sha256": risk.sha256, "parameter_search": False},
    )
    shadow = pd.read_csv(args.shadow_intents, dtype={"symbol": str})
    source = load_frozen_intents(shadow)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    confirmed = confirm_followthrough_intents(panel, source, config)
    followthrough = pd.DataFrame([asdict(item) for item in confirmed])
    if followthrough.empty:
        raise RuntimeError("no follow-through intents were confirmed")
    experiments = [
        ("legacy_full_period", shadow),
        (config.strategy_version, followthrough),
    ]
    rows = []
    for name, frame in experiments:
        result, audit = run_experiment(
            panel, frame, name, risk, args.start_date, args.end_date,
            ranking_column="score")
        rows.append(result)
        _save_audit(args.output_dir / name, audit)
        print(name, json.dumps(result, ensure_ascii=False), flush=True)
    results = pd.DataFrame(rows)
    results.to_csv(args.output_dir / "results.csv", index=False)
    followthrough.to_csv(args.output_dir / "confirmed_intents.csv.gz",
                         index=False, compression="gzip")
    indexed = results.set_index("experiment")
    baseline = indexed.loc["legacy_full_period"]
    candidate = indexed.loc[config.strategy_version]
    comparison = {
        "strategy_version": config.strategy_version,
        "config_sha256": config.sha256,
        "registration_sha256": registration["record_sha256"],
        "source_intents": len(source), "confirmed_intents": len(confirmed),
        "confirmation_rate": len(confirmed)/len(source),
        "return_delta_pct_points": float(
            candidate.return_pct-baseline.return_pct),
        "max_drawdown_delta_pct_points": float(
            candidate.max_drawdown_pct-baseline.max_drawdown_pct),
        "mean_r_delta": float(candidate.mean_r-baseline.mean_r),
        "filled_buy_delta": int(candidate.filled_buys-baseline.filled_buys),
        "parameter_search_performed": False,
        "research_only": True,
    }
    (args.output_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    result_id = trial_id+"-result"
    _register(registry, result_id, "Frozen result for "+trial_id,
              {"config_sha256": config.sha256}, status="OBSERVED",
              observed_metrics=comparison)
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

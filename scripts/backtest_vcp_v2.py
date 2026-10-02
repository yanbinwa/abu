#!/usr/bin/env python3
"""Run frozen VCP C/D/E/F experiments and attention ablations."""
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
from abupy.AlphaBu.ABuVCPStrategy import (  # noqa: E402
    VCP_EXPERIMENTS, load_vcp_attention_config, load_vcp_core_config,
    run_vcp_backtest,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path,
                        default=Path("/Users/wjy/abu/backtests/vcp_v2"))
    parser.add_argument("--years", type=int, nargs="+",
                        default=[2022, 2023, 2024, 2025, 2026])
    parser.add_argument("--experiments", nargs="+", choices=VCP_EXPERIMENTS,
                        default=list(VCP_EXPERIMENTS))
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--continuous", action="store_true",
                        help="also run one unreset path spanning all requested years")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    core = load_vcp_core_config(ROOT / "configs/selection/vcp_core_v1.json")
    attention = load_vcp_attention_config(
        ROOT / "configs/selection/vcp_attention_v1.json")
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=min(args.years) * 10000 - 10000,
        end_date=max(args.years) * 10000 + 1231,
    )
    rows = []
    for year in args.years:
        for experiment in args.experiments:
            result, curve, fills, decisions, exits = run_vcp_backtest(
                panel, year, experiment, args.slippage_bps,
                core, attention, risk)
            rows.append(result)
            directory = args.output_dir / "{}_{}".format(experiment, year)
            directory.mkdir(parents=True, exist_ok=True)
            curve.to_csv(directory / "daily_nav.csv", index=False)
            fills.to_csv(directory / "fills.csv", index=False)
            pd.DataFrame(exits).to_csv(directory / "exit_reasons.csv", index=False)
            with (directory / "risk_decisions.jsonl").open("w") as output:
                for decision in decisions:
                    output.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
            print(experiment, year, "return", round(result["return_pct"], 3),
                  "drawdown", round(result["max_drawdown_pct"], 3), flush=True)
    if args.continuous:
        for experiment in args.experiments:
            result, curve, fills, decisions, exits = run_vcp_backtest(
                panel, None, experiment, args.slippage_bps, core, attention, risk,
                start_date=min(args.years) * 10000 + 101,
                end_date=max(args.years) * 10000 + 1231,
            )
            rows.append(result)
            directory = args.output_dir / (experiment + "_continuous")
            directory.mkdir(parents=True, exist_ok=True)
            curve.to_csv(directory / "daily_nav.csv", index=False)
            fills.to_csv(directory / "fills.csv", index=False)
            pd.DataFrame(exits).to_csv(directory / "exit_reasons.csv", index=False)
            with (directory / "risk_decisions.jsonl").open("w") as output:
                for decision in decisions:
                    output.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
    pd.DataFrame(rows).to_csv(args.output_dir / "results.csv", index=False)
    manifest = {
        "engine": "vcp_v2", "years": args.years,
        "experiments": args.experiments, "slippage_bps": args.slippage_bps,
        "core_config": asdict(core), "core_config_sha256": core.sha256,
        "attention_config": asdict(attention),
        "attention_config_sha256": attention.sha256,
        "risk_config_sha256": risk.sha256,
        "warning": "research output; completion does not imply live admission",
    }
    (args.output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

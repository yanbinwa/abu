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
    load_vcp_residual_config, run_vcp_backtest,
)
from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore  # noqa: E402


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
    parser.add_argument("--continuous-only", action="store_true",
                        help="run only unreset paths spanning all requested years")
    parser.add_argument("--sync-dynamic-stops", action="store_true",
                        help="publish executable trailing stops to risk sizing")
    parser.add_argument("--execution-policy", choices=("D0", "M1", "M2"),
                        default="D0")
    parser.add_argument("--minute-store", type=Path)
    parser.add_argument("--minute-source")
    args = parser.parse_args()
    if args.execution_policy != "D0" and args.minute_store is None:
        parser.error("--minute-store is required for M1/M2")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    core = load_vcp_core_config(ROOT / "configs/selection/vcp_core_v1.json")
    attention = load_vcp_attention_config(
        ROOT / "configs/selection/vcp_attention_v1.json")
    residual = load_vcp_residual_config(
        ROOT / "configs/selection/vcp_residual_v2.json")
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=min(args.years) * 10000 - 10000,
        end_date=max(args.years) * 10000 + 1231,
    )
    minute_store = (MinuteBarStore(args.minute_store)
                    if args.minute_store is not None else None)
    minute_loader = (None if minute_store is None else
                     lambda date, symbol: minute_store.read(
                         symbol, date, 1, source=args.minute_source))
    rows = []
    if not args.continuous_only:
        for year in args.years:
            for experiment in args.experiments:
                result, curve, fills, decisions, exits = run_vcp_backtest(
                    panel, year, experiment, args.slippage_bps,
                    core, attention, risk, residual,
                    sync_dynamic_stops=args.sync_dynamic_stops,
                    execution_policy_id=args.execution_policy,
                    minute_bars=minute_loader)
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
    if args.continuous or args.continuous_only:
        for experiment in args.experiments:
            audit = {}
            result, curve, fills, decisions, exits = run_vcp_backtest(
                panel, None, experiment, args.slippage_bps, core, attention, risk,
                residual,
                start_date=min(args.years) * 10000 + 101,
                end_date=max(args.years) * 10000 + 1231,
                audit=audit,
                sync_dynamic_stops=args.sync_dynamic_stops,
                execution_policy_id=args.execution_policy,
                minute_bars=minute_loader,
            )
            rows.append(result)
            directory = args.output_dir / (experiment + "_continuous")
            directory.mkdir(parents=True, exist_ok=True)
            curve.to_csv(directory / "daily_nav.csv", index=False)
            fills.to_csv(directory / "fills.csv", index=False)
            pd.DataFrame(exits).to_csv(directory / "exit_reasons.csv", index=False)
            for name in ("intents", "orders", "reservations", "position_events",
                         "unexit_positions"):
                records = audit[name]
                if records and hasattr(records[0], "__dataclass_fields__"):
                    records = [asdict(item) for item in records]
                pd.DataFrame(records).to_csv(directory / (name + ".csv"), index=False)
            with (directory / "intents.jsonl").open("w") as output:
                for intent in audit["intents"]:
                    output.write(json.dumps(asdict(intent), ensure_ascii=False) + "\n")
            with (directory / "risk_decisions.jsonl").open("w") as output:
                for decision in decisions:
                    output.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
            print(experiment, "continuous", "return",
                  round(result["return_pct"], 3), "drawdown",
                  round(result["max_drawdown_pct"], 3), flush=True)
    pd.DataFrame(rows).to_csv(args.output_dir / "results.csv", index=False)
    manifest = {
        "engine": "vcp_v2", "years": args.years,
        "experiments": args.experiments, "slippage_bps": args.slippage_bps,
        "continuous": args.continuous or args.continuous_only,
        "continuous_only": args.continuous_only,
        "dynamic_stop_sync": bool(args.sync_dynamic_stops),
        "execution_policy_id": args.execution_policy,
        "minute_store": (str(args.minute_store)
                         if args.minute_store is not None else None),
        "minute_source": args.minute_source,
        "core_config": asdict(core), "core_config_sha256": core.sha256,
        "attention_config": asdict(attention),
        "attention_config_sha256": attention.sha256,
        "residual_config": asdict(residual),
        "residual_config_sha256": residual.sha256,
        "risk_config_sha256": risk.sha256,
        "warning": "research output; completion does not imply live admission",
    }
    (args.output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run A1/A2 and strictly separated B1/B2 selection experiments."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuSelectionStrategiesV2 import (  # noqa: E402
    EXPERIMENT_MODES, LEGACY_STRATEGIES, load_b1_config,
    run_legacy_v2_backtest,
)


def _git_revision():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return result.stdout.strip() or "unknown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path,
                        default=Path("/Users/wjy/abu/backtests/selection_v2"))
    parser.add_argument("--years", type=int, nargs="+",
                        default=[2022, 2023, 2024, 2025, 2026])
    parser.add_argument("--strategies", nargs="+", choices=LEGACY_STRATEGIES,
                        default=list(LEGACY_STRATEGIES))
    parser.add_argument("--modes", nargs="+", choices=EXPERIMENT_MODES,
                        default=list(EXPERIMENT_MODES))
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    risk_config = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    b1_config = load_b1_config(ROOT / "configs/selection/b1_constraints_v1.json")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=min(args.years) * 10000 - 10000,
        end_date=max(args.years) * 10000 + 1231,
    )
    rows = []
    for year in args.years:
        for strategy in args.strategies:
            for mode in args.modes:
                label = "{}_{}_{}".format(strategy, year, mode)
                directory = args.output_dir / label
                directory.mkdir(parents=True, exist_ok=True)
                result, curve, fills, decisions = run_legacy_v2_backtest(
                    panel, strategy, year, mode=mode,
                    slippage_bps=args.slippage_bps,
                    risk_config=risk_config, b1_config=b1_config,
                )
                rows.append(result)
                curve.to_csv(directory / "daily_nav.csv", index=False)
                fills.to_csv(directory / "fills.csv", index=False)
                with (directory / "risk_decisions.jsonl").open("w") as output:
                    for decision in decisions:
                        payload = asdict(decision) if is_dataclass(decision) else decision
                        output.write(json.dumps(payload, ensure_ascii=False) + "\n")
                print(label, "return", round(result["return_pct"], 3),
                      "drawdown", round(result["max_drawdown_pct"], 3), flush=True)

    results = pd.DataFrame(rows)
    baseline = results[results.experiment == "a2_pit_corrected"][
        ["source_strategy", "year", "return_pct"]
    ].rename(columns={"return_pct": "a2_return_pct"})
    results = results.merge(baseline, on=["source_strategy", "year"], how="left")
    results["return_retention_ratio"] = (
        results.return_pct / results.a2_return_pct.replace(0, pd.NA)
    )
    results.to_csv(args.output_dir / "results.csv", index=False)

    snapshot = args.research_dir / "snapshot_manifest.json"
    snapshot_id = None
    if snapshot.exists():
        snapshot_id = json.loads(snapshot.read_text()).get("snapshot_id")
    manifest = {
        "engine": "selection_v2", "code_commit": _git_revision(),
        "data_snapshot_id": snapshot_id,
        "risk_config_sha256": risk_config.sha256,
        "b1_config_sha256": b1_config.sha256,
        "slippage_bps": args.slippage_bps,
        "years": args.years, "strategies": args.strategies,
        "modes": args.modes,
        "a0_baseline_commit": "460ef76",
        "a0_status": "frozen_failure_record",
        "b1_semantics": "market-value constraints; no stop and no R",
        "b2_semantics": "new rstop_v2 strategy versions with executable stops",
    }
    (args.output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (args.output_dir / "risk_v1.json").write_text(
        json.dumps(asdict(risk_config), ensure_ascii=False, indent=2) + "\n"
    )
    (args.output_dir / "b1_constraints_v1.json").write_text(
        json.dumps(asdict(b1_config), ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()

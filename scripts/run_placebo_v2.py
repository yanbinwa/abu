#!/usr/bin/env python3
"""Run resumable point-in-time matched placebos through the v2 executor."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuMatchedPlaceboV2 import (  # noqa: E402
    MatchedPlaceboV2, PlaceboConfig, summarize_placebos,
)
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import (  # noqa: E402
    PortfolioRiskEngine, load_risk_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuSelectionStrategiesV2 import (  # noqa: E402
    B1ConstraintEngine, load_b1_config, rebuild_legacy_placebo_intent,
)
from abupy.AlphaBu.ABuTradeIntent import TradeIntent  # noqa: E402
from abupy.AlphaBu.ABuVCPStrategy import (  # noqa: E402
    VCP_EXIT_PROFILES, VCPStrategy, load_vcp_attention_config,
    load_vcp_core_config, load_vcp_residual_config, make_vcp_exit_engine,
)


def _read_intents(path):
    text = Path(path).read_text(encoding="utf-8")
    records = json.loads(text) if text.lstrip().startswith("[") else [
        json.loads(line) for line in text.splitlines() if line.strip()
    ]
    allowed = {item.name for item in fields(TradeIntent)}
    intents = []
    for record in records:
        unknown = set(record) - allowed
        if unknown:
            raise ValueError("unknown TradeIntent fields: {}".format(sorted(unknown)))
        record = dict(record)
        for key in ("required_fields", "missing_fields"):
            if key in record:
                record[key] = tuple(record[key])
        intents.append(TradeIntent(**record))
    return intents


def _atomic_csv(frame, path):
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", type=int, default=20200101)
    parser.add_argument("--end-date", type=int, default=20261002)
    parser.add_argument("--replicates", type=int, default=1000)
    parser.add_argument("--replicate-start", type=int, default=0)
    parser.add_argument("--replicate-end", type=int,
                        help="exclusive replicate id; defaults to --replicates")
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--pool-size", type=int, default=50)
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--actual-return-pct", type=float)
    parser.add_argument("--approval-mode", choices=("fixed", "b1", "full"),
                        default="full")
    parser.add_argument("--event-exit", action="store_true",
                        help="deprecated alias for --exit-profile full_event_v1")
    parser.add_argument("--exit-profile", choices=VCP_EXIT_PROFILES,
                        help="VCP exit profile; defaults to fixed20")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    if args.replicates <= 0:
        raise ValueError("replicates must be positive")
    if args.event_exit and args.exit_profile not in (None, "full_event_v1"):
        raise ValueError("--event-exit conflicts with --exit-profile")
    exit_profile = ("full_event_v1" if args.event_exit else
                    args.exit_profile or "fixed20")
    replicate_end = (args.replicates if args.replicate_end is None
                     else args.replicate_end)
    if args.replicate_start < 0 or replicate_end <= args.replicate_start:
        raise ValueError("invalid replicate range")
    if replicate_end > args.replicates:
        raise ValueError("replicate end exceeds total replicates")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    distribution_path = args.output_dir / "placebo_distribution.csv"
    if args.fresh and distribution_path.exists():
        distribution_path.unlink()
    existing = (pd.read_csv(distribution_path)
                if distribution_path.exists() else pd.DataFrame())
    completed = (set(existing.replicate.astype(int))
                 if not existing.empty else set())

    intents = _read_intents(args.intents)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=args.start_date, end_date=args.end_date,
    )
    config = PlaceboConfig(
        pool_size=args.pool_size, replicates=args.replicates, seed=args.seed
    )
    runner = MatchedPlaceboV2(panel, config)
    execution = ExecutionConfig(slippage_bps=args.slippage_bps,
                                max_positions=args.max_positions)
    is_vcp = bool(intents) and all(
        intent.r_definition_version == "vcp_structure_or_2atr_v1"
        for intent in intents)
    if is_vcp:
        core = load_vcp_core_config(ROOT / "configs/selection/vcp_core_v1.json")
        attention = load_vcp_attention_config(
            ROOT / "configs/selection/vcp_attention_v1.json")
        residual = load_vcp_residual_config(
            ROOT / "configs/selection/vcp_residual_v2.json")
        vcp = VCPStrategy(panel, core, attention, residual)
        rebuild = vcp.rebuild_placebo_intent
        exit_factory = (None if exit_profile == "fixed20" else
                        lambda current_panel: make_vcp_exit_engine(
                            current_panel, exit_profile))
    else:
        rebuild = rebuild_legacy_placebo_intent
        exit_factory = None
    if args.approval_mode == "full":
        risk_engine = PortfolioRiskEngine(
            panel, load_risk_config(ROOT / "configs/selection/risk_v1.json"))
        approve = risk_engine.approve
    elif args.approval_mode == "b1":
        b1_engine = B1ConstraintEngine(
            panel, load_b1_config(ROOT / "configs/selection/b1_constraints_v1.json"))
        def approve(executor, intent, day, entry_day):
            max_price = float(intent.metadata["max_buy_price_raw"])
            target = float(intent.metadata.get("target_notional", 40_000))
            requested = int(target / max_price / 100) * 100
            return b1_engine.approve(executor, intent, day, entry_day, requested)
    else:
        approve = None
    expected = replicate_end - args.replicate_start
    for replicate in range(args.replicate_start, replicate_end):
        if replicate in completed:
            continue
        row = runner.run_distribution(
            intents, replicate_ids=[replicate], rebuild=rebuild,
            execution_config=execution, approve=approve,
            exit_policy_factory=exit_factory,
        )
        existing = pd.concat([existing, row], ignore_index=True)
        existing = existing.drop_duplicates("replicate", keep="last").sort_values(
            "replicate").reset_index(drop=True)
        _atomic_csv(existing, distribution_path)
        completed_in_range = int(existing.replicate.between(
            args.replicate_start, replicate_end-1).sum())
        print("completed {}/{} in range [{}:{})".format(
            completed_in_range, expected, args.replicate_start, replicate_end),
            flush=True)

    missing = Counter(field for intent in intents for field in intent.missing_fields)
    summary = summarize_placebos(existing, args.actual_return_pct)
    summary.update({
        "engine": "matched_placebo_v2",
        "seed": args.seed,
        "pool_size": args.pool_size,
        "slippage_bps": args.slippage_bps,
        "max_positions": args.max_positions,
        "approval_mode": args.approval_mode,
        "event_exit": exit_profile == "full_event_v1",
        "exit_profile": exit_profile,
        "intent_count": len(intents),
        "replicate_range": [args.replicate_start, replicate_end],
        "missing_field_counts": dict(sorted(missing.items())),
        "signal_start": int(panel.dates[0]),
        "signal_end": int(panel.dates[-1]),
    })
    summary = _json_safe(summary)
    (args.output_dir / "placebo_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

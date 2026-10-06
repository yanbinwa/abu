#!/usr/bin/env python3
"""Paired portfolio replay of a frozen retrospective market-emotion gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuShortLineCloseFactors import (  # noqa: E402
    MarketSentimentEntryGate, MarketSentimentTransitionEntryGate,
    build_close_event_features, load_complete_history,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import _save_audit, rank_frame  # noqa: E402


DEFAULT_CONFIG = ROOT / "configs/selection/shortline_market_entry_gate_v1.json"
DEFAULT_HISTORY = Path(
    "/Users/wjy/abu/data/selection_research/eltdx_close_history_v1")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_canonical_price_volume_persistence_v2_20261006/"
    "oos_predictions.csv.gz")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def validate_config(config):
    allowed_statuses = {
        "RETROSPECTIVE_SCREEN_ONLY_NOT_ADMITTED",
        "POST_HOC_MECHANISM_DIAGNOSTIC_NOT_ADMITTED",
    }
    if config["research_status"] not in allowed_statuses:
        raise ValueError("entry gate must remain retrospective research")
    if config["strict_pit"] is not False or \
            config["availability_evidence"] != "BACKFILLED_QUERY":
        raise ValueError("backfilled history cannot be declared strict PIT")
    if config["parameter_search"] is not False or \
            config["automatic_admission"] is not False:
        raise ValueError("frozen gate cannot search or auto-admit")
    gate = config["gate"]
    level = {
        "rule": "sentiment_z63_lte_and_risk_z63_gte",
        "sentiment_z63_max": -1.0,
        "risk_z63_min": 1.0,
        "action": "suppress_new_review_entries_only",
        "rank_exits_unchanged": True,
        "existing_positions_unchanged": True,
    }
    transition = {
        "rule": "risk_off_level_and_one_day_deterioration",
        "sentiment_z63_max": -1.0,
        "risk_z63_min": 1.0,
        "sentiment_transition": "strictly_lower_than_previous_session",
        "risk_transition": "strictly_higher_than_previous_session",
        "action": "suppress_new_review_entries_only",
        "rank_exits_unchanged": True,
        "existing_positions_unchanged": True,
    }
    if gate not in (level, transition):
        raise ValueError("market gate differs from frozen registration")
    if gate == transition and config.get("post_hoc_hypothesis") is not True:
        raise ValueError("transition diagnostic must declare post-hoc origin")


def make_gate(config, daily):
    gate = config["gate"]
    gate_type = (MarketSentimentTransitionEntryGate
                 if gate["rule"] ==
                 "risk_off_level_and_one_day_deterioration"
                 else MarketSentimentEntryGate)
    return gate_type(daily, gate["sentiment_z63_max"], gate["risk_z63_min"])


def _daily_returns(curve, initial_cash=1_000_000.0):
    capital = np.r_[float(initial_cash), curve.capital.to_numpy(dtype=float)]
    return capital[1:] / capital[:-1] - 1.0


def paired_path_bootstrap(base_curve, gated_curve, block, replicates, seed):
    if not np.array_equal(base_curve.date.to_numpy(),
                          gated_curve.date.to_numpy()):
        raise ValueError("paired curves have different dates")
    base = _daily_returns(base_curve)
    gated = _daily_returns(gated_curve)
    if len(base) != len(gated) or not len(base):
        raise ValueError("paired curves are empty or unequal")
    block = max(1, min(int(block), len(base)))
    blocks = int(math.ceil(len(base) / block))
    offsets = np.arange(block)
    rng = np.random.default_rng(int(seed))
    return_delta = np.empty(int(replicates), dtype=float)
    drawdown_improvement = np.empty(int(replicates), dtype=float)
    for index in range(int(replicates)):
        starts = rng.integers(0, len(base), size=blocks)
        positions = ((starts[:, None] + offsets) % len(base)).ravel()[:len(base)]
        base_path = np.cumprod(1.0 + base[positions])
        gate_path = np.cumprod(1.0 + gated[positions])
        return_delta[index] = (gate_path[-1] - base_path[-1]) * 100.0
        base_dd = (base_path / np.maximum.accumulate(base_path) - 1).min()
        gate_dd = (gate_path / np.maximum.accumulate(gate_path) - 1).min()
        drawdown_improvement[index] = (gate_dd - base_dd) * 100.0
    return {
        "return_delta_ci95_low": float(np.quantile(return_delta, .025)),
        "return_delta_ci95_high": float(np.quantile(return_delta, .975)),
        "return_delta_probability_positive": float(
            (return_delta > 0).mean()),
        "drawdown_improvement_ci95_low": float(
            np.quantile(drawdown_improvement, .025)),
        "drawdown_improvement_ci95_high": float(
            np.quantile(drawdown_improvement, .975)),
        "drawdown_improvement_probability_positive": float(
            (drawdown_improvement > 0).mean()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/shortline_market_entry_gate_v1_20261006"))
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    validate_config(config)
    start, end = config["primary_overlap_start"], config["primary_overlap_end"]
    events, sessions, manifests = load_complete_history(
        args.history_dir, start, end)
    expected_months = set(pd.period_range(
        str(start), str(end), freq="M").astype(str).str.replace("-", ""))
    complete_months = {str(item["month"]) for item in manifests}
    missing_months = sorted(expected_months - complete_months)
    if missing_months:
        raise RuntimeError("complete event months missing: {}".format(
            ",".join(missing_months)))
    daily, _ = build_close_event_features(events, sessions)

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if policy.strategy_version != config["source_strategy_version"]:
        raise ValueError("source strategy version differs from registration")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source and policy config hashes differ")
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", config["score_column"]],
        dtype={"symbol": str})
    predictions = predictions[predictions.signal_asof.between(start, end)]
    missing_dates = sorted(set(predictions.signal_asof.astype(int)) - set(sessions))
    if missing_dates:
        raise RuntimeError("complete event sessions missing: {}".format(
            ",".join(str(value) for value in missing_dates[:20])))
    execution_score = policy.score_column
    if config["score_column"] != execution_score:
        predictions = predictions.rename(
            columns={config["score_column"]: execution_score})
    ranked = rank_frame(
        predictions, execution_score,
        max(policy.entry_rank_limit, policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101, end_date=end)

    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)
    write_json(args.output_dir / "registration.json", {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config,
        "config_sha256": digest(args.config),
        "predictions_path": str(args.predictions),
        "predictions_sha256": digest(args.predictions),
        "source_config_sha256": source.sha256,
        "policy_config_sha256": policy.sha256,
        "risk_config_sha256": risk.sha256,
        "history_batches": [{
            "month": item["month"], "batch_id": item["batch_id"],
            "parsed_sha256": item["parsed_sha256"],
        } for item in manifests],
        "automatic_admission": False,
    })
    daily.to_csv(args.output_dir / "daily_event_factors.csv", index=False)

    results, comparisons, annual = [], [], []
    gate_config = config["gate"]
    for offset, cost in enumerate(config["slippage_bps"]):
        paired = {}
        for arm in ("baseline", "market_entry_gate"):
            gate = None if arm == "baseline" else make_gate(config, daily)
            key = "{}_{}bp".format(arm, int(cost))
            result, audit = run_low_turnover(
                panel, ranked, replace(source, label_slippage_bps=float(cost)),
                policy, risk, end, review_overlay=gate)
            result.update({"arm": arm, "key": key,
                           "slippage_bps": float(cost)})
            results.append(result)
            yearly = annual_returns(audit["curve"])
            yearly.insert(0, "key", key)
            annual.append(yearly)
            _save_audit(args.output_dir / key, audit)
            if gate is not None:
                pd.DataFrame(gate.evaluations).to_csv(
                    args.output_dir / key / "gate_evaluations.csv", index=False)
            paired[arm] = (result, audit, gate)
            print(json.dumps({
                "key": key, "return_pct": result["return_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "filled_buys": result["filled_buys"],
            }), flush=True)
        base, gate = paired["baseline"], paired["market_entry_gate"]
        gate_rows = gate[2].evaluations
        comparison = {
            "slippage_bps": float(cost),
            "return_delta_pct_points": float(
                gate[0]["return_pct"] - base[0]["return_pct"]),
            "max_drawdown_improvement_pct_points": float(
                gate[0]["max_drawdown_pct"] - base[0]["max_drawdown_pct"]),
            "expected_shortfall_improvement_pct_points": float(
                gate[0]["daily_expected_shortfall_95_pct"] -
                base[0]["daily_expected_shortfall_95_pct"]),
            "average_exposure_delta_pct_points": float(
                gate[0]["average_exposure_pct"] -
                base[0]["average_exposure_pct"]),
            "filled_buy_delta": int(
                gate[0]["filled_buys"] - base[0]["filled_buys"]),
            "gate_review_evaluations": int(len(gate_rows)),
            "risk_off_review_evaluations": int(sum(
                item["risk_off"] for item in gate_rows)),
            "suppressed_entry_candidates": int(sum(
                item["suppressed_entries"] for item in gate_rows)),
        }
        comparison.update(paired_path_bootstrap(
            base[1]["curve"], gate[1]["curve"],
            config["bootstrap_block_sessions"],
            config["bootstrap_replicates"], 20261006 + offset))
        comparisons.append(comparison)

    result_frame = pd.DataFrame(results)
    comparison_frame = pd.DataFrame(comparisons)
    result_frame.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    comparison_frame.to_csv(args.output_dir / "paired_comparisons.csv", index=False)
    pd.concat(annual, ignore_index=True).to_csv(
        args.output_dir / "annual_returns.csv", index=False)
    gate_trigger_dates = make_gate(config, daily).trigger_dates()
    trigger_frame = daily[daily.signal_asof.isin(gate_trigger_dates)]
    trigger_frame.to_csv(
        args.output_dir / "gate_trigger_dates.csv", index=False)
    all_costs_positive = bool((comparison_frame.return_delta_pct_points > 0).all())
    all_drawdowns_better = bool((
        comparison_frame.max_drawdown_improvement_pct_points >= 0).all())
    report = {
        "experiment_id": config["experiment_id"],
        "role": config["research_status"],
        "date_start": int(ranked.signal_asof.min()),
        "date_end": int(ranked.signal_asof.max()),
        "complete_sessions": int(len(sessions)),
        "gate_trigger_dates": int(len(gate_trigger_dates)),
        "paired_comparisons": comparisons,
        "directionally_positive_all_costs": (
            all_costs_positive and all_drawdowns_better),
        "decision": "KEEP_FORWARD_SHADOW_ONLY",
        "automatic_admission": False,
        "warning": "The close-event history was queried retrospectively in "
                   "2026 and cannot establish strict PIT performance. A "
                   "post-hoc transition hypothesis remains diagnostic only.",
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "complete",
        "completed_at": datetime.now().astimezone().isoformat(),
        "report_sha256": digest(args.output_dir / "report.json"),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

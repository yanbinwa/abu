#!/usr/bin/env python3
"""Frozen 2025-2026 BaoStock valuation overlay validation for Alpha158."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuArtifactManifest import sha256_file  # noqa: E402
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    daily_rank_ic, daily_selection_uplift, moving_block_mean_interval,
)


DEFAULT_CONFIG = (
    ROOT / "configs/selection/alpha158_baostock_fundamental_overlay_v1.json")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/current_strategy_comparison_20261004_v2/"
    "inputs/alpha.csv.gz")
DEFAULT_SIGNAL = Path("/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal")
DEFAULT_RESEARCH = Path(
    "/Users/wjy/abu/shadow/alpha158_forward_v1/data/research")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_baostock_fundamental_overlay_2025_2026_20261006")


def stable_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str)


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")


def provider_code(symbol):
    value = str(symbol).lower()
    if len(value) != 8 or value[:2] not in {"sh", "sz"}:
        raise ValueError("invalid symbol " + value)
    return value[:2] + "." + value[2:]


def fetch_symbol(api, symbol, fields, start_date, end_date):
    response = api.query_history_k_data_plus(
        provider_code(symbol), ",".join(fields),
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag="3")
    if response.error_code != "0":
        raise RuntimeError(response.error_code + ":" + response.error_msg)
    rows = []
    while response.next():
        rows.append(dict(zip(response.fields, response.get_row_data())))
    return rows


def _collect_chunk(symbols, config, raw_dir, worker):
    import baostock as bs

    fields = ["date", "code", *config["value_fields"],
              "tradestatus", "isST"]
    raw_dir = Path(raw_dir)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError("BaoStock login failed: " + login.error_msg)
    failures = []
    try:
        for index, symbol in enumerate(symbols, 1):
            path = raw_dir / (symbol + ".jsonl")
            if path.exists():
                continue
            try:
                rows = fetch_symbol(
                    bs, symbol, fields, config["download_start_date"],
                    config["download_end_date"])
                path.write_text(
                    "".join(stable_json(row) + "\n" for row in rows),
                    encoding="utf-8")
            except Exception as error:
                failures.append({
                    "symbol": symbol, "error_type": type(error).__name__,
                    "error": str(error),
                })
            if index == 1 or index % 25 == 0 or index == len(symbols):
                print(json.dumps({
                    "worker": worker, "downloaded": index,
                    "worker_total": len(symbols),
                    "failures": len(failures), "symbol": symbol,
                }), flush=True)
    finally:
        bs.logout()
    return failures


def collect_valuations(symbols, config, raw_dir, workers):
    raw_dir.mkdir(parents=True, exist_ok=True)
    pending = [symbol for symbol in symbols
               if not (raw_dir / (symbol + ".jsonl")).exists()]
    if not pending:
        return []
    workers = max(1, min(int(workers), len(pending)))
    if workers == 1:
        return _collect_chunk(pending, config, raw_dir, 1)
    chunks = [pending[index::workers] for index in range(workers)]
    failures = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(
            _collect_chunk, chunk, config, raw_dir, index + 1)
            for index, chunk in enumerate(chunks) if chunk]
        for future in as_completed(futures):
            failures.extend(future.result())
    return failures


def read_valuations(symbols, raw_dir, value_fields):
    rows = []
    for symbol in symbols:
        path = raw_dir / (symbol + ".jsonl")
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append({"symbol": symbol, **json.loads(line)})
    columns = ["symbol", "date", *value_fields, "tradestatus", "isST"]
    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame.date).dt.strftime("%Y%m%d").astype(int)
    for field in value_fields:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        frame.loc[~np.isfinite(frame[field]) | (frame[field] <= 0), field] = np.nan
    return frame.sort_values(["date", "symbol"]).drop_duplicates(
        ["date", "symbol"], keep="last")


def value_composite(frame, value_fields, minimum_valid, missing_percentile):
    result = frame.copy()
    ranks = []
    for field in value_fields:
        name = field + "_yield_rank"
        values = 1.0 / pd.to_numeric(result[field], errors="coerce")
        values = values.where(np.isfinite(values) & (values > 0))
        result[name] = values.groupby(result.signal_asof).rank(
            method="average", pct=True)
        ranks.append(name)
    result["valid_value_fields"] = result[ranks].notna().sum(axis=1)
    result["fundamental_percentile"] = result[ranks].mean(axis=1)
    insufficient = result.valid_value_fields < int(minimum_valid)
    result.loc[insufficient, "fundamental_percentile"] = float(
        missing_percentile)
    result["fundamental_data_eligible"] = ~insufficient
    return result


def lag_date_map(signal_dates, panel_dates, delay_sessions):
    positions = {int(value): index for index, value in enumerate(panel_dates)}
    mapping = {}
    for value in signal_dates:
        date = int(value)
        position = positions[date] - int(delay_sessions)
        mapping[date] = int(panel_dates[position]) if position >= 0 else None
    return mapping


def make_overlay(top, valuations, config, panel_dates, delay_sessions):
    frame = top.copy()
    mapping = lag_date_map(
        frame.signal_asof.unique(), panel_dates, delay_sessions)
    frame["valuation_date"] = frame.signal_asof.map(mapping)
    values = valuations.rename(columns={"date": "valuation_date"})
    frame = frame.merge(
        values[["symbol", "valuation_date", *config["value_fields"]]],
        on=["symbol", "valuation_date"], how="left", validate="many_to_one")
    frame = value_composite(
        frame, config["value_fields"],
        config["minimum_valid_value_fields"],
        config["missing_fundamental_percentile"])
    depth = int(config["candidate_depth"])
    frame["alpha_rank_percentile"] = (
        depth + 1 - frame.daily_rank.astype(float)) / depth
    frame["overlay_score"] = (
        float(config["alpha_rank_weight"]) * frame.alpha_rank_percentile +
        float(config["fundamental_rank_weight"]) *
        frame.fundamental_percentile)
    ranked = rank_frame(
        frame[["signal_asof", "symbol", "column", "overlay_score"]],
        "overlay_score", depth)
    return frame, ranked.rename(columns={"overlay_score": "alpha_score"})


def records_frame(records):
    if isinstance(records, pd.DataFrame):
        return records
    return pd.DataFrame([
        asdict(row) if is_dataclass(row) else row for row in records])


def save_portfolio(output, name, result, audit):
    directory = output / name
    directory.mkdir()
    audit["curve"].to_csv(directory / "daily_nav.csv", index=False)
    audit["fills"].to_csv(directory / "fills.csv", index=False)
    records_frame(audit["selection"]).to_csv(
        directory / "selection_decisions.csv", index=False)
    write_json(directory / "metrics.json", result)


def paired_portfolio_diagnostics(base_curve, candidate_curve, seed):
    paired = base_curve[["date", "capital"]].rename(
        columns={"capital": "baseline_capital"}).merge(
        candidate_curve[["date", "capital"]].rename(
            columns={"capital": "candidate_capital"}), on="date",
        validate="one_to_one")
    paired["baseline_daily_return"] = paired.baseline_capital.pct_change()
    paired["candidate_daily_return"] = paired.candidate_capital.pct_change()
    paired["daily_return_delta"] = (
        paired.candidate_daily_return - paired.baseline_daily_return)
    values = paired.daily_return_delta.dropna()
    ci = moving_block_mean_interval(
        values, block_length=20, replicates=3000, seed=seed)
    return paired, [float(ci[0]), float(ci[1])]


def factor_diagnostics(frame, candidate_column):
    base = daily_rank_ic(frame, "alpha_score").rename(
        columns={"ic": "baseline_ic"})
    candidate = daily_rank_ic(frame, candidate_column).rename(
        columns={"ic": "candidate_ic"})
    paired = base.merge(candidate, on="signal_asof", validate="one_to_one")
    paired["ic_delta"] = paired.candidate_ic - paired.baseline_ic
    base_uplift = daily_selection_uplift(
        frame, "alpha_score", 10)[["signal_asof", "uplift"]].rename(
            columns={"uplift": "baseline_uplift"})
    candidate_uplift = daily_selection_uplift(
        frame, candidate_column, 10)[["signal_asof", "uplift"]].rename(
            columns={"uplift": "candidate_uplift"})
    uplift = base_uplift.merge(
        candidate_uplift, on="signal_asof", validate="one_to_one")
    uplift["uplift_delta"] = (
        uplift.candidate_uplift - uplift.baseline_uplift)
    return paired, uplift


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config["parameter_search_performed"] or config["promotion_allowed"]:
        raise ValueError("research-only frozen contract changed")
    if not np.isclose(
            config["alpha_rank_weight"] + config["fundamental_rank_weight"],
            1.0):
        raise ValueError("overlay weights must sum to one")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "raw_baostock"
    registration = {
        "config": config,
        "config_sha256": hashlib.sha256(stable_json(config).encode()).hexdigest(),
        "registered_before_download": True,
        "input_hashes": {
            str(args.predictions): sha256_file(args.predictions),
            str(args.source_config): sha256_file(args.source_config),
            str(args.policy_config): sha256_file(args.policy_config),
            str(args.risk_config): sha256_file(args.risk_config),
        },
    }
    registration_path = output / "registration.json"
    if registration_path.exists():
        existing = json.loads(registration_path.read_text(encoding="utf-8"))
        if existing != registration:
            raise ValueError("existing registration differs")
    else:
        write_json(registration_path, registration)

    prediction_columns = [
        "signal_asof", "symbol", "column", "alpha_score",
        "excess_return_20d", "target_rank", "train_end"]
    predictions = pd.read_csv(
        args.predictions, usecols=prediction_columns, dtype={"symbol": str})
    predictions = predictions[
        predictions.signal_asof.between(
            int(config["signal_start_date"]),
            int(config["signal_end_date"]))].copy()
    if predictions.empty or not (
            predictions.train_end < predictions.signal_asof).all():
        raise ValueError("valid OOS predictions required")
    depth = int(config["candidate_depth"])
    baseline_scores = rank_frame(predictions, "alpha_score", depth)
    targets = predictions[[
        "signal_asof", "symbol", "excess_return_20d", "target_rank"]]
    top = baseline_scores.merge(
        targets, on=["signal_asof", "symbol"], validate="one_to_one")
    symbols = sorted(top.symbol.unique())
    failures = collect_valuations(symbols, config, raw_dir, args.workers)
    write_json(output / "download_failures.json", failures)
    valuations = read_valuations(symbols, raw_dir, config["value_fields"])
    valuations.to_csv(output / "baostock_valuations.csv.gz", index=False)

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("policy source hash mismatch")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(config["signal_end_date"]))

    overlay_frames, score_frames = {}, {"baseline": baseline_scores}
    for name, delay in (("raw", 0),
                        ("delayed20", int(config["stress_delay_sessions"]))):
        overlay, scores = make_overlay(
            top, valuations, config, panel.dates, delay)
        overlay_frames[name] = overlay
        score_frames[name] = scores
        overlay.to_csv(output / (name + "_factor_panel.csv.gz"), index=False)

    portfolio_results, portfolio_audits, annual = {}, {}, {}
    for name, scores in score_frames.items():
        result, audit = run_low_turnover(
            panel, scores, source, policy, risk,
            int(config["signal_end_date"]),
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        portfolio_results[name] = result
        portfolio_audits[name] = audit
        annual[name] = annual_returns(audit["curve"]).assign(strategy=name)
        save_portfolio(output, name, result, audit)
    pd.concat(annual.values(), ignore_index=True).to_csv(
        output / "annual_returns.csv", index=False)

    factor_summary = {}
    for name, overlay in overlay_frames.items():
        paired_ic, uplift = factor_diagnostics(overlay, "overlay_score")
        paired_ic.to_csv(output / (name + "_paired_daily_ic.csv"), index=False)
        uplift.to_csv(output / (name + "_paired_top10_uplift.csv"), index=False)
        ic_ci = moving_block_mean_interval(
            paired_ic.ic_delta, 20, 3000, 20261006)
        factor_summary[name] = {
            "eligible_row_coverage": float(
                overlay.fundamental_data_eligible.mean()),
            "median_valid_value_fields": float(
                overlay.valid_value_fields.median()),
            "baseline_rank_ic_mean": float(paired_ic.baseline_ic.mean()),
            "candidate_rank_ic_mean": float(paired_ic.candidate_ic.mean()),
            "rank_ic_delta_mean": float(paired_ic.ic_delta.mean()),
            "rank_ic_delta_block_ci": [float(ic_ci[0]), float(ic_ci[1])],
            "baseline_top10_uplift_mean": float(
                uplift.baseline_uplift.mean()),
            "candidate_top10_uplift_mean": float(
                uplift.candidate_uplift.mean()),
            "top10_uplift_delta_mean": float(uplift.uplift_delta.mean()),
            "top10_mean_overlap": float(overlay.assign(
                base_top=overlay.daily_rank <= 10,
                candidate_rank=overlay.groupby("signal_asof").overlay_score.rank(
                    ascending=False, method="first"),
            ).groupby("signal_asof").apply(
                lambda group: float((group.base_top &
                                     (group.candidate_rank <= 10)).sum()/10)
            ).mean()),
        }

    paired_summary = {}
    for index, name in enumerate(("raw", "delayed20")):
        paired, ci = paired_portfolio_diagnostics(
            portfolio_audits["baseline"]["curve"],
            portfolio_audits[name]["curve"], 20261006 + index)
        paired.to_csv(output / (name + "_paired_daily_returns.csv"), index=False)
        paired_summary[name] = {
            "return_delta_pct_points": float(
                portfolio_results[name]["return_pct"] -
                portfolio_results["baseline"]["return_pct"]),
            "max_drawdown_delta_pct_points": float(
                portfolio_results[name]["max_drawdown_pct"] -
                portfolio_results["baseline"]["max_drawdown_pct"]),
            "daily_return_delta_block_ci": ci,
        }

    annual_table = pd.concat(annual.values(), ignore_index=True)
    pivot = annual_table.pivot(index="year", columns="strategy",
                               values="return_pct")
    raw_positive_years = int((pivot["raw"] - pivot["baseline"] > 0).sum())
    gates = config["acceptance_gates"]
    checks = {
        "raw_return_delta": paired_summary["raw"][
            "return_delta_pct_points"] > gates[
                "raw_return_delta_pct_points_min"],
        "raw_drawdown": paired_summary["raw"][
            "max_drawdown_delta_pct_points"] >= -float(
                gates["raw_max_drawdown_worsening_pct_points_max"]),
        "delayed_return_delta": paired_summary["delayed20"][
            "return_delta_pct_points"] > gates[
                "delayed_return_delta_pct_points_min"],
        "annual_consistency": raw_positive_years >= int(
            gates["positive_annual_delta_min_count"]),
        "paired_daily_ci": paired_summary["raw"][
            "daily_return_delta_block_ci"][0] >= gates[
                "paired_daily_return_delta_block_ci_low_min"],
    }
    report = {
        "experiment_id": config["experiment_id"],
        "research_label": config["research_label"],
        "period": [int(predictions.signal_asof.min()),
                   int(predictions.signal_asof.max())],
        "prediction_dates": int(predictions.signal_asof.nunique()),
        "candidate_symbols": len(symbols),
        "baostock_symbols_with_data": int(valuations.symbol.nunique()),
        "download_failure_count": len(failures),
        "portfolio_results": portfolio_results,
        "paired_portfolio": paired_summary,
        "factor_diagnostics": factor_summary,
        "annual_returns": annual_table.to_dict("records"),
        "positive_raw_annual_delta_count": raw_positive_years,
        "acceptance_checks": checks,
        "research_gate": "PASS" if all(checks.values()) else "FAIL",
        "promotion_decision": "NOT_ALLOWED_RESEARCH_ONLY",
        "pit_admission": "FAIL_NO_HISTORICAL_REVISION_LINEAGE",
        "parameter_search_performed": False,
        "limitations": config["source_limitations"],
    }
    write_json(output / "report.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE",
        "report_sha256": hashlib.sha256(stable_json(report).encode()).hexdigest(),
        "research_gate": report["research_gate"],
        "promotion_decision": report["promotion_decision"],
    })
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate Alpha158 on full A-share eligibility using audited BaoStock ST."""
from __future__ import annotations

import argparse
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
    Alpha158LiteFeatureEngine, Alpha158LiteModel,
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuArtifactManifest import sha256_file  # noqa: E402
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    daily_rank_ic, daily_selection_uplift, moving_block_mean_interval,
)


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_full_market_st_v1.json"
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_full_market_st_2025_2026_20261006")
DEFAULT_SIGNAL = Path("/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal")
DEFAULT_RESEARCH = Path(
    "/Users/wjy/abu/shadow/alpha158_forward_v1/data/research")


def stable_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str)


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")


def validate_st_source(config):
    panel_path = Path(config["st_panel"])
    audit_path = Path(config["st_audit"])
    if sha256_file(panel_path) != config["st_panel_sha256"]:
        raise ValueError("ST panel hash mismatch")
    if sha256_file(audit_path) != config["st_audit_sha256"]:
        raise ValueError("ST audit hash mismatch")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("gate_status") != "PASS_ST_EXCLUSION_DATA_GATE":
        raise ValueError("ST exclusion gate is not passed")
    if audit.get("exact_st_coverage") != 1.0:
        raise ValueError("ST exclusion coverage is incomplete")
    if any(value.get("coverage") != 1.0
           for value in audit.get("exchange_coverage", {}).values()):
        raise ValueError("exchange ST coverage is incomplete")
    return audit


def generate_predictions(panel, source, config, output):
    split = WalkForwardConfig(
        minimum_train_dates=source.minimum_train_dates,
        validation_dates=source.validation_dates,
        test_dates=source.test_dates,
        label_horizon_sessions=source.label_horizon_sessions,
        embargo_sessions=0,
    )
    engine = Alpha158LiteFeatureEngine(panel, source)
    positions = np.arange(len(panel.dates))
    signal_days = np.flatnonzero(
        (panel.dates >= int(config["training_signal_start_date"])) &
        (panel.dates <= int(config["evaluation_end_date"])) &
        (positions >= source.minimum_history_sessions))
    predictions, manifests = [], []
    for fold in PurgedWalkForward(split).split(
            panel.dates[signal_days], panel.dates):
        if int(fold.test_end) < int(config["evaluation_start_date"]):
            continue
        train_days = signal_days[fold.train_indices][
            ::source.training_date_stride]
        training = pd.concat(
            [engine.snapshot(int(day), include_labels=True)
             for day in train_days], ignore_index=True)
        model = Alpha158LiteModel(source).fit(
            training, panel.dates[train_days])
        manifest = dict(model.manifest)
        manifest.update({
            "fold": int(fold.fold), "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
        })
        manifests.append(manifest)
        for position in fold.test_indices:
            day = int(signal_days[position])
            date = int(panel.dates[day])
            if date < int(config["evaluation_start_date"]):
                continue
            snapshot = engine.snapshot(day, include_labels=True)
            if snapshot.empty:
                continue
            frame = snapshot[[
                "signal_asof", "symbol", "column", "baseline_score",
                "excess_return_20d", "target_rank",
            ]].copy()
            frame["alpha_score"] = model.predict(snapshot)
            frame["fold"] = int(fold.fold)
            frame["train_end"] = int(fold.train_end)
            predictions.append(frame)
        del training
        print(json.dumps({
            "fold": int(fold.fold), "train_dates": int(len(train_days)),
            "train_rows": int(manifest["train_rows"]),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
        }), flush=True)
    if not predictions:
        raise RuntimeError("walk-forward produced no evaluation predictions")
    result = pd.concat(predictions, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("walk-forward test folds overlap")
    write_json(output / "fold_manifests.json", manifests)
    return result, engine, split


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


def predictive_summary(frame, score):
    ic = daily_rank_ic(frame, score)
    uplift = daily_selection_uplift(frame, score, 10)
    ci = moving_block_mean_interval(
        ic.ic, block_length=20, replicates=3000, seed=20261006)
    exchange = frame.assign(exchange=frame.symbol.str[:2]).groupby(
        ["signal_asof", "exchange"], sort=True).apply(
            lambda group: group[score].corr(
                group.excess_return_20d, method="spearman")
        ).rename("ic").dropna().reset_index()
    yearly = ic.assign(year=ic.signal_asof // 10000).groupby(
        "year", sort=True).agg(
            dates=("ic", "size"), mean_ic=("ic", "mean"),
            positive_rate=("ic", lambda values: float((values > 0).mean())),
        ).reset_index()
    return {
        "rows": int(len(frame)),
        "dates": int(frame.signal_asof.nunique()),
        "symbols": int(frame.symbol.nunique()),
        "median_candidates": float(
            frame.groupby("signal_asof").size().median()),
        "mean_rank_ic": float(ic.ic.mean()),
        "rank_ic_block_ci": [float(ci[0]), float(ci[1])],
        "top10_uplift_mean": float(uplift.uplift.mean()),
        "year_metrics": yearly.to_dict("records"),
        "exchange_mean_ic": exchange.groupby("exchange").ic.mean().to_dict(),
    }, ic, uplift


def paired_portfolio(base_curve, candidate_curve, seed):
    frame = base_curve[["date", "capital"]].rename(
        columns={"capital": "baseline_capital"}).merge(
        candidate_curve[["date", "capital"]].rename(
            columns={"capital": "candidate_capital"}),
        on="date", validate="one_to_one")
    frame["baseline_return"] = frame.baseline_capital.pct_change()
    frame["candidate_return"] = frame.candidate_capital.pct_change()
    frame["return_delta"] = frame.candidate_return - frame.baseline_return
    ci = moving_block_mean_interval(
        frame.return_delta.dropna(), 20, 3000, seed)
    return frame, [float(ci[0]), float(ci[1])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config["parameter_search_performed"] or config["promotion_allowed"]:
        raise ValueError("frozen research contract changed")
    st_audit = validate_st_source(config)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    registration = {
        "experiment": config,
        "experiment_sha256": hashlib.sha256(
            stable_json(config).encode()).hexdigest(),
        "registered_before_model_fit": True,
        "input_hashes": {
            config["st_panel"]: sha256_file(config["st_panel"]),
            config["st_audit"]: sha256_file(config["st_audit"]),
            config["baseline_predictions"]: sha256_file(
                config["baseline_predictions"]),
            str(args.source_config): sha256_file(args.source_config),
            str(args.policy_config): sha256_file(args.policy_config),
            str(args.risk_config): sha256_file(args.risk_config),
        },
    }
    write_json(output / "registration.json", registration)

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.strategy_version != config["source_strategy_version"]:
        raise ValueError("source strategy mismatch")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("policy source hash mismatch")
    baseline_report = json.loads(Path(
        config["baseline_research_report"]).read_text(encoding="utf-8"))
    if baseline_report["config_sha256"] != source.sha256:
        raise ValueError("baseline model config mismatch")

    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(config["evaluation_end_date"]),
        st_exclusion_panel=config["st_panel"])
    candidate, engine, split = generate_predictions(
        panel, source, config, output)
    candidate.to_csv(
        output / "full_market_oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})

    baseline_columns = [
        "signal_asof", "symbol", "column", "alpha_score",
        "excess_return_20d", "target_rank", "train_end"]
    baseline = pd.read_csv(
        config["baseline_predictions"], usecols=baseline_columns,
        dtype={"symbol": str})
    baseline = baseline[baseline.signal_asof.between(
        int(config["evaluation_start_date"]),
        int(config["evaluation_end_date"]))].copy()
    if not (baseline.train_end < baseline.signal_asof).all():
        raise ValueError("baseline contains non-OOS predictions")
    for row in pd.concat([
            baseline[["symbol", "column"]],
            candidate[["symbol", "column"]]], ignore_index=True
            ).drop_duplicates().itertuples():
        if panel.symbols[int(row.column)] != row.symbol:
            raise ValueError("symbol-column mapping mismatch")

    prediction_frames = {
        "frozen_shenzhen_baseline": baseline,
        "full_market_model_shenzhen_only": candidate[
            candidate.symbol.str.startswith("sz")].copy(),
        "full_market_model_all_a_share": candidate,
    }
    predictive, scores = {}, {}
    for name, frame in prediction_frames.items():
        summary, ic, uplift = predictive_summary(frame, "alpha_score")
        predictive[name] = summary
        ic.to_csv(output / (name + "_daily_ic.csv"), index=False)
        uplift.to_csv(output / (name + "_top10_uplift.csv"), index=False)
        scores[name] = rank_frame(
            frame[["signal_asof", "symbol", "column", "alpha_score"]],
            "alpha_score", max(
                policy.entry_rank_limit, policy.retention_rank_limit))

    results, audits, annual = {}, {}, {}
    for name, ranked in scores.items():
        result, audit = run_low_turnover(
            panel, ranked, source, policy, risk,
            int(config["evaluation_end_date"]),
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        results[name] = result
        audits[name] = audit
        annual[name] = annual_returns(audit["curve"]).assign(strategy=name)
        save_portfolio(output, name, result, audit)
    annual_table = pd.concat(annual.values(), ignore_index=True)
    annual_table.to_csv(output / "annual_returns.csv", index=False)

    baseline_name = "frozen_shenzhen_baseline"
    full_name = "full_market_model_all_a_share"
    paired, paired_ci = paired_portfolio(
        audits[baseline_name]["curve"], audits[full_name]["curve"],
        20261006)
    paired.to_csv(output / "paired_daily_returns.csv", index=False)
    annual_pivot = annual_table.pivot(
        index="year", columns="strategy", values="return_pct")
    annual_deltas = annual_pivot[full_name] - annual_pivot[baseline_name]
    delta = {
        "return_pct_points": float(
            results[full_name]["return_pct"] -
            results[baseline_name]["return_pct"]),
        "max_drawdown_pct_points": float(
            results[full_name]["max_drawdown_pct"] -
            results[baseline_name]["max_drawdown_pct"]),
        "liquidation_return_pct_points": float(
            results[full_name]["liquidation_3_limits_return_pct"] -
            results[baseline_name]["liquidation_3_limits_return_pct"]),
        "paired_daily_return_delta_block_ci": paired_ci,
        "annual_return_deltas": {
            str(int(year)): float(value)
            for year, value in annual_deltas.items()},
    }
    gates = config["acceptance_gates"]
    checks = {
        "factor_ic": predictive[full_name]["rank_ic_block_ci"][0] >= float(
            gates["full_market_rank_ic_block_ci_low_min"]),
        "return": delta["return_pct_points"] > float(
            gates["return_delta_pct_points_min"]),
        "drawdown": delta["max_drawdown_pct_points"] >= -float(
            gates["max_drawdown_worsening_pct_points_max"]),
        "liquidation": delta["liquidation_return_pct_points"] >= float(
            gates["liquidation_return_delta_pct_points_min"]),
        "annual_consistency": int((annual_deltas > 0).sum()) >= int(
            gates["positive_annual_delta_min_count"]),
        "paired_daily_ci": paired_ci[0] >= float(
            gates["paired_daily_return_delta_block_ci_low_min"]),
    }
    report = {
        "experiment_id": config["experiment_id"],
        "period": [int(candidate.signal_asof.min()),
                   int(candidate.signal_asof.max())],
        "st_exclusion_audit": {
            "gate_status": st_audit["gate_status"],
            "exact_st_coverage": st_audit["exact_st_coverage"],
            "exchange_coverage": st_audit["exchange_coverage"],
            "source_role": st_audit["source_role"],
        },
        "feature_config_sha256": engine.feature_config_sha256,
        "walk_forward": asdict(split),
        "predictive_results": predictive,
        "portfolio_results": results,
        "annual_returns": annual_table.to_dict("records"),
        "full_market_delta_vs_frozen_baseline": delta,
        "acceptance_checks": checks,
        "research_gate": "PASS" if all(checks.values()) else "FAIL",
        "promotion_decision": "NOT_ALLOWED_RESEARCH_ONLY",
        "parameter_search_performed": False,
        "limitations": config["limitations"],
    }
    write_json(output / "report.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE",
        "report_sha256": hashlib.sha256(
            stable_json(report).encode()).hexdigest(),
        "research_gate": report["research_gate"],
    })
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

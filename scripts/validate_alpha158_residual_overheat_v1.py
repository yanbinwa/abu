#!/usr/bin/env python3
"""Validate one preregistered residual-trend and overheat feature family."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    ALPHA158_LITE_FEATURES, Alpha158LiteFeatureEngine, Alpha158LiteModel,
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
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
from scripts.validate_alpha158_ranker_nonlinear_v1 import (  # noqa: E402
    canonical_hash, write_json,
)


EXTRA_FEATURES = (
    "residual_momentum_6_1_standardized",
    "industry_excess_60d_standardized",
    "max_return_20d",
    "overheat_5d_amount",
    "gap_range_overheat",
)
ALL_FEATURES = (*ALPHA158_LITE_FEATURES, *EXTRA_FEATURES)


def _safe_divide(numerator, denominator):
    numerator = np.asarray(numerator, dtype=float)
    denominator = np.asarray(denominator, dtype=float)
    result = np.full(np.broadcast_shapes(numerator.shape, denominator.shape),
                     np.nan, dtype=float)
    np.divide(numerator, denominator, out=result,
              where=np.isfinite(numerator) & np.isfinite(denominator) &
              (denominator != 0))
    return result


def _rank(values):
    values = np.asarray(values, dtype=float)
    result = np.full(values.shape, np.nan, dtype=np.float32)
    valid = np.isfinite(values)
    if valid.any():
        result[valid] = (
            pd.Series(values[valid]).rank(method="average", pct=True)
            .to_numpy(dtype=np.float32) - .5)
    return result


class ResidualOverheatFeatures:
    def __init__(self, panel, source):
        self.panel = panel
        self.source = source
        self.base = Alpha158LiteFeatureEngine(panel, source)
        self.denominator = panel.breadth_denominator(
            min_history=120, unknown_st_policy=source.unknown_st_policy)

    def _residual_momentum(self, day):
        size = len(self.panel.symbols)
        result = np.full(size, np.nan, dtype=float)
        regression_start, regression_end = day - 251, day - 20
        formation_start = day - 125
        if regression_start < 0 or formation_start < 0:
            return result
        stock = np.asarray(
            self.panel.returns[regression_start:regression_end], float)
        market = np.asarray(
            self.panel.benchmark_returns[regression_start:regression_end], float)
        valid = np.isfinite(stock) & np.isfinite(market[:, None])
        observations = valid.sum(axis=0)
        market_matrix = market[:, None]
        market_mean = _safe_divide(
            np.where(valid, market_matrix, 0).sum(axis=0), observations)
        stock_mean = _safe_divide(
            np.where(valid, stock, 0).sum(axis=0), observations)
        numerator = np.where(
            valid, (market_matrix-market_mean)*(stock-stock_mean), 0
        ).sum(axis=0)
        denominator = np.where(
            valid, (market_matrix-market_mean)**2, 0).sum(axis=0)
        beta = _safe_divide(numerator, denominator)

        formation = np.asarray(
            self.panel.returns[formation_start:regression_end], float)
        formation_market = np.asarray(
            self.panel.benchmark_returns[formation_start:regression_end], float)
        formation_valid = (
            np.isfinite(formation) & np.isfinite(formation_market[:, None]))
        residual = formation - beta[None, :] * formation_market[:, None]
        count = formation_valid.sum(axis=0)
        mean = _safe_divide(
            np.where(formation_valid, residual, 0).sum(axis=0), count)
        centered = np.where(formation_valid, residual-mean, 0)
        volatility = np.sqrt(_safe_divide(
            (centered**2).sum(axis=0), count-1))
        result = _safe_divide(mean, volatility)
        result[(observations < 200) | (count < 95)] = np.nan
        return result

    def _industry_excess(self, day):
        current = np.asarray(self.panel.close[day], float)
        return60 = _safe_divide(current, self.panel.close[day-60]) - 1
        daily = np.asarray(self.panel.returns[day-59:day+1], float)
        count = np.isfinite(daily).sum(axis=0)
        volatility = np.nanstd(daily, axis=0, ddof=1)
        volatility[count < 48] = np.nan
        industries = np.asarray(self.panel.industry[day], dtype=int)
        baseline = np.full(len(industries), np.nan, dtype=float)
        eligible = self.denominator[day]
        for industry in np.unique(industries[eligible & (industries >= 0)]):
            members = eligible & (industries == industry)
            values = return60[members]
            values = values[np.isfinite(values)]
            if len(values):
                baseline[members] = float(np.mean(values))
        return _safe_divide(return60-baseline, volatility)

    def snapshot(self, day):
        frame = self.base.snapshot(day, include_labels=True)
        if frame.empty:
            return frame
        columns = frame.column.to_numpy(dtype=int)
        returns20 = np.asarray(self.panel.returns[day-19:day+1], float)
        valid = np.isfinite(returns20)
        max_return = np.max(
            np.where(valid, returns20, -np.inf), axis=0)
        max_return[valid.sum(axis=0) < 16] = np.nan
        return5 = _safe_divide(
            self.panel.close[day], self.panel.close[day-5]) - 1
        amount20 = np.asarray(self.panel.amount[day-19:day+1], float)
        amount_ratio = _safe_divide(
            self.panel.amount[day], np.nanmedian(amount20, axis=0))
        amount_shock = np.where(
            np.isfinite(amount_ratio) & (amount_ratio > 1),
            np.log(amount_ratio), 0)
        overheat = np.maximum(return5, 0) * amount_shock
        previous = np.asarray(self.panel.close[day-1], float)
        gap = _safe_divide(self.panel.open[day], previous) - 1
        range_fraction = _safe_divide(
            self.panel.high[day]-self.panel.low[day], previous)
        gap_range = np.maximum(gap, 0) * np.maximum(range_fraction, 0)
        raw = {
            "residual_momentum_6_1_standardized":
                self._residual_momentum(day),
            "industry_excess_60d_standardized":
                self._industry_excess(day),
            "max_return_20d": max_return,
            "overheat_5d_amount": overheat,
            "gap_range_overheat": gap_range,
        }
        for name in EXTRA_FEATURES:
            frame[name] = _rank(raw[name][columns])
        return frame


def fit_extended_ridge(frame, source):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline

    rows = frame.dropna(subset=["target_rank"])
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("model", Ridge(alpha=source.ridge_alpha)),
    ])
    date_count = rows.groupby("signal_asof").signal_asof.transform("size")
    weights = len(rows) / rows.signal_asof.nunique() / date_count
    pipeline.fit(
        rows[list(ALL_FEATURES)], rows.target_rank,
        model__sample_weight=weights.to_numpy(dtype=float))
    return pipeline, {
        "model": "median_imputer_plus_ridge",
        "ridge_alpha": float(source.ridge_alpha),
        "features": list(ALL_FEATURES),
        "train_rows": int(len(rows)),
        "train_dates": int(rows.signal_asof.nunique()),
        "sample_weighting": "equal_total_weight_per_signal_date",
    }


def generate_predictions(panel, source, research, output):
    walk = research["walk_forward"]
    split = WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(walk["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]),
    )
    features = ResidualOverheatFeatures(panel, source)
    positions = np.arange(len(panel.dates))
    signal_days = np.flatnonzero(
        (panel.dates >= int(walk["start_date"])) &
        (panel.dates <= int(walk["end_date"])) &
        (positions >= source.minimum_history_sessions))
    cache, predictions, manifests = {}, [], []
    for fold in PurgedWalkForward(split).split(
            panel.dates[signal_days], panel.dates):
        train_days = signal_days[fold.train_indices][
            ::int(walk["training_date_stride"])]
        frames = []
        for day in train_days:
            key = int(day)
            if key not in cache:
                cache[key] = features.snapshot(key)
            frames.append(cache[key])
        training = pd.concat(frames, ignore_index=True)
        baseline = Alpha158LiteModel(source).fit(
            training, panel.dates[train_days])
        candidate, candidate_manifest = fit_extended_ridge(training, source)
        manifests.append({
            "fold": int(fold.fold), "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "validation_start": int(fold.validation_start),
            "validation_end": int(fold.validation_end),
            "test_start": int(fold.test_start), "test_end": int(fold.test_end),
            "label_horizon_sessions": int(fold.label_horizon_sessions),
            "additional_embargo_sessions": int(
                fold.additional_embargo_sessions),
            "train_label_end": int(fold.train_label_end),
            "validation_label_end": int(fold.validation_label_end),
            "train_to_validation_clear_sessions": int(
                fold.train_to_validation_clear_sessions),
            "validation_to_test_clear_sessions": int(
                fold.validation_to_test_clear_sessions),
            "strict_label_non_overlap": True,
            "baseline": baseline.manifest, "candidate": candidate_manifest,
        })
        for position in fold.test_indices:
            day = int(signal_days[position])
            snapshot = features.snapshot(day)
            if snapshot.empty:
                continue
            values = snapshot[[
                "signal_asof", "symbol", "column", "excess_return_20d",
                "target_rank",
            ]].copy()
            values["ridge_score"] = baseline.predict(snapshot)
            values["candidate_score"] = candidate.predict(
                snapshot[list(ALL_FEATURES)])
            values["fold"] = int(fold.fold)
            values["train_end"] = int(fold.train_end)
            predictions.append(values)
        print(json.dumps({
            "fold": int(fold.fold), "train_dates": int(len(train_days)),
            "train_rows": int(len(training)),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
        }), flush=True)
    result = pd.concat(predictions, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("walk-forward test folds overlap")
    write_json(output / "fold_manifests.json", manifests)
    return result, features, split


def evaluate_factor(predictions, gates):
    baseline = daily_rank_ic(predictions, "ridge_score").rename(
        columns={"ic": "ridge_ic"})
    candidate = daily_rank_ic(predictions, "candidate_score").rename(
        columns={"ic": "candidate_ic"})
    paired = baseline.merge(candidate, on="signal_asof", validate="one_to_one")
    paired["ic_delta"] = paired.candidate_ic - paired.ridge_ic
    ci = moving_block_mean_interval(
        paired.ic_delta, block_length=20, replicates=2000, seed=20261006)
    base_uplift = daily_selection_uplift(
        predictions, "ridge_score", 10)[["signal_asof", "uplift"]].rename(
            columns={"uplift": "ridge_uplift"})
    candidate_uplift = daily_selection_uplift(
        predictions, "candidate_score", 10)[["signal_asof", "uplift"]].rename(
            columns={"uplift": "candidate_uplift"})
    uplift = base_uplift.merge(
        candidate_uplift, on="signal_asof", validate="one_to_one")
    uplift["uplift_delta"] = uplift.candidate_uplift-uplift.ridge_uplift
    uplift_ci = moving_block_mean_interval(
        uplift.uplift_delta, block_length=20, replicates=2000,
        seed=20261006)
    paired["year"] = paired.signal_asof // 10000
    yearly = paired.groupby("year", sort=True).agg(
        dates=("ic_delta", "size"), ridge_mean_ic=("ridge_ic", "mean"),
        candidate_mean_ic=("candidate_ic", "mean"),
        ic_delta=("ic_delta", "mean")).reset_index()
    positive_years = int((yearly.ic_delta > 0).sum())
    passed = (
        ci[0] >= float(gates["paired_ic_delta_block_ci_low_min"]) and
        uplift.uplift_delta.mean() > float(gates["top10_uplift_delta_min"]) and
        uplift_ci[0] >= float(gates.get(
            "top10_uplift_delta_block_ci_low_min", -float("inf"))) and
        positive_years >= int(gates["positive_year_ic_delta_min_count"])
    )
    report = {
        "ridge_rank_ic_mean": float(paired.ridge_ic.mean()),
        "candidate_rank_ic_mean": float(paired.candidate_ic.mean()),
        "paired_ic_delta_mean": float(paired.ic_delta.mean()),
        "paired_ic_delta_block_ci": [float(ci[0]), float(ci[1])],
        "ridge_top10_uplift_mean": float(uplift.ridge_uplift.mean()),
        "candidate_top10_uplift_mean": float(uplift.candidate_uplift.mean()),
        "top10_uplift_delta_mean": float(uplift.uplift_delta.mean()),
        "top10_uplift_delta_block_ci": [
            float(uplift_ci[0]), float(uplift_ci[1])],
        "positive_year_ic_delta_count": positive_years,
        "factor_gate": "PASS" if passed else "FAIL",
    }
    return report, paired, uplift, yearly


def run_portfolios(panel, predictions, source, policy, risk, end_date):
    results, annual = {}, {}
    for name, column in (("ridge", "ridge_score"),
                         ("candidate", "candidate_score")):
        frame = predictions[[
            "signal_asof", "symbol", "column", column]].rename(
                columns={column: "alpha_score"})
        scores = rank_frame(frame, "alpha_score", max(
            policy.entry_rank_limit, policy.retention_rank_limit))
        result, audit = run_low_turnover(
            panel, scores, source, policy, risk, end_date,
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        results[name] = result
        annual[name] = annual_returns(audit["curve"]).to_dict("records")
    return results, annual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_residual_overheat_research_v1.json")
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_residual_overheat_v1_20261005"))
    args = parser.parse_args()

    research = json.loads(args.research_config.read_text(encoding="utf-8"))
    if tuple(research["feature_family"]) != EXTRA_FEATURES:
        raise ValueError("feature family differs from preregistration")
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.strategy_version != research["source_strategy_version"]:
        raise ValueError("source strategy mismatch")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("policy source hash mismatch")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "registration.json", {
        "experiment": research,
        "experiment_sha256": canonical_hash(research),
        "source_config": asdict(source),
        "source_config_sha256": source.sha256,
        "policy_config": asdict(policy),
        "policy_config_sha256": policy.sha256,
        "risk_config_sha256": risk.sha256,
        "baseline_features": list(ALPHA158_LITE_FEATURES),
        "candidate_features": list(ALL_FEATURES),
        "registered_before_results": True,
    })

    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(research["walk_forward"]["end_date"]))
    predictions, feature_engine, split = generate_predictions(
        panel, source, research, output)
    factor, paired, uplift, yearly = evaluate_factor(
        predictions, research["gates"])
    predictions.to_csv(
        output / "oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    paired.to_csv(output / "paired_daily_ic.csv", index=False)
    uplift.to_csv(output / "paired_top10_uplift.csv", index=False)
    yearly.to_csv(output / "year_metrics.csv", index=False)

    portfolio, annual = run_portfolios(
        panel, predictions, source, policy, risk,
        int(research["walk_forward"]["end_date"]))
    baseline, candidate = portfolio["ridge"], portfolio["candidate"]
    comparison = {
        "return_delta_pct_points": float(
            candidate["return_pct"]-baseline["return_pct"]),
        "max_drawdown_delta_pct_points": float(
            candidate["max_drawdown_pct"]-baseline["max_drawdown_pct"]),
        "liquidation_return_delta_pct_points": float(
            candidate["liquidation_3_limits_return_pct"]-
            baseline["liquidation_3_limits_return_pct"]),
        "average_exposure_delta_pct_points": float(
            candidate["average_exposure_pct"]-
            baseline["average_exposure_pct"]),
        "filled_buy_delta": int(
            candidate["filled_buys"]-baseline["filled_buys"]),
    }
    gates = research["gates"]
    portfolio_pass = (
        comparison["return_delta_pct_points"] >
        float(gates["portfolio_return_delta_pct_points_min"]) and
        comparison["max_drawdown_delta_pct_points"] >=
        -float(gates["max_drawdown_worsening_limit_pct_points"]) and
        comparison["liquidation_return_delta_pct_points"] >
        float(gates["liquidation_return_delta_pct_points_min"])
    )
    decision = (
        "RETAIN_FOR_NEW_FORWARD_SHADOW_ONLY"
        if factor["factor_gate"] == "PASS" and portfolio_pass
        else "REJECT_HISTORICAL_SCREEN")
    report = {
        "experiment_id": research["experiment_id"],
        "role": research["research_status"],
        "base_feature_config_sha256":
            feature_engine.base.feature_config_sha256,
        "label_config_sha256": feature_engine.base.label_config_sha256,
        "split": asdict(split),
        "oos_rows": int(len(predictions)),
        "oos_dates": int(predictions.signal_asof.nunique()),
        "factor": factor, "portfolio": portfolio,
        "portfolio_comparison": comparison, "annual_returns": annual,
        "portfolio_gate": "PASS" if portfolio_pass else "FAIL",
        "decision": decision,
        "parameter_search_performed": False,
        "observed_history": True, "new_holdout": False,
        "warning": (
            "Observed history only. A pass may justify a separately "
            "registered forward shadow but never immediate adoption."),
    }
    write_json(output / "report.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE", "decision": decision,
        "report_sha256": canonical_hash(report),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

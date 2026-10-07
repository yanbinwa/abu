#!/usr/bin/env python3
"""Fixed-count selection and weak-year attribution for Ridge label ablation."""
from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuResearchStatistics import (  # noqa: E402
    attribute_daily_pnl, classify_market_regimes,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.MLBu.ABuMLPortfolioEvaluator import moving_block_indices  # noqa: E402


DEFAULT_ROOT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_ridge_policy_label_ablation_v1_20261007")
ARMS = {
    "ridge_excess20_common_v1": "excess20_ridge_score",
    "ridge_event_r60_v1": "event_r60_ridge_score",
}
BASELINE, CANDIDATE = tuple(ARMS)


def summarize_fixed_count_day(frame):
    """Select first, then evaluate labels; never backfill missing outcomes."""
    result = {"signal_asof": int(frame.signal_asof.iloc[0])}
    selections = {}
    for arm, score in ARMS.items():
        ordered = frame.sort_values(
            [score, "symbol"], ascending=[False, True], kind="mergesort")
        selections[arm] = {}
        for name, selected in (
                ("top10", ordered.head(10)),
                ("top20", ordered.head(20)),
                ("top11_20", ordered.iloc[10:20])):
            valid = selected.event_path_r_60d.dropna()
            result["{}_{}_selected".format(arm, name)] = int(len(selected))
            result["{}_{}_labeled".format(arm, name)] = int(len(valid))
            result["{}_{}_coverage".format(arm, name)] = (
                float(len(valid)/len(selected)) if len(selected) else np.nan)
            result["{}_{}_mean_r".format(arm, name)] = (
                float(valid.mean()) if len(valid) else np.nan)
            result["{}_{}_median_r".format(arm, name)] = (
                float(valid.median()) if len(valid) else np.nan)
            result["{}_{}_win_rate".format(arm, name)] = (
                float(valid.gt(0).mean()) if len(valid) else np.nan)
            selections[arm][name] = set(selected.symbol)
    for name in ("top10", "top20", "top11_20"):
        left, right = selections[BASELINE][name], selections[CANDIDATE][name]
        result[name+"_overlap"] = len(left & right)/max(1, len(left))
    return result


def _paired_block_interval(values, block, paths=5000, seed=20261007):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    indices = moving_block_indices(len(values), block, paths, seed)
    means = values[indices].mean(axis=1)
    return {"block_sessions": int(block), "paths": int(paths),
            "lower": float(np.quantile(means, .025)),
            "median": float(np.quantile(means, .5)),
            "upper": float(np.quantile(means, .975))}


def fixed_count_analysis(predictions, output):
    usecols = ["signal_asof", "symbol", "event_path_r_60d", *ARMS.values()]
    records, carry = [], None
    for chunk in pd.read_csv(predictions, usecols=usecols, chunksize=250_000,
                             dtype={"symbol": str}):
        if carry is not None:
            chunk = pd.concat([carry, chunk], ignore_index=True)
        last_date = int(chunk.signal_asof.iloc[-1])
        carry = chunk.loc[chunk.signal_asof.eq(last_date)].copy()
        complete = chunk.loc[~chunk.signal_asof.eq(last_date)]
        records.extend(summarize_fixed_count_day(group) for _, group in
                       complete.groupby("signal_asof", sort=False))
    if carry is not None and len(carry):
        records.append(summarize_fixed_count_day(carry))
    daily = pd.DataFrame(records).sort_values("signal_asof").reset_index(drop=True)
    daily.to_csv(output/"fixed_count_daily.csv", index=False)
    summaries, bootstrap = [], {}
    for band in ("top10", "top20", "top11_20"):
        baseline = daily["{}_{}_mean_r".format(BASELINE, band)]
        candidate = daily["{}_{}_mean_r".format(CANDIDATE, band)]
        difference = candidate-baseline
        summaries.append({
            "band": band,
            "baseline_mean_daily_r": float(baseline.mean()),
            "candidate_mean_daily_r": float(candidate.mean()),
            "mean_daily_r_uplift": float(difference.mean()),
            "baseline_median_daily_r": float(baseline.median()),
            "candidate_median_daily_r": float(candidate.median()),
            "baseline_mean_win_rate": float(daily[
                "{}_{}_win_rate".format(BASELINE, band)].mean()),
            "candidate_mean_win_rate": float(daily[
                "{}_{}_win_rate".format(CANDIDATE, band)].mean()),
            "baseline_label_coverage": float(daily[
                "{}_{}_coverage".format(BASELINE, band)].mean()),
            "candidate_label_coverage": float(daily[
                "{}_{}_coverage".format(CANDIDATE, band)].mean()),
            "mean_selection_overlap": float(daily[band+"_overlap"].mean()),
            "candidate_better_date_rate": float(difference.gt(0).mean()),
        })
        bootstrap[band] = {
            str(block): _paired_block_interval(difference, block)
            for block in (20, 40, 60)}
    summary = pd.DataFrame(summaries)
    summary.to_csv(output/"fixed_count_summary.csv", index=False)
    yearly = []
    for year, group in daily.groupby(daily.signal_asof//10000):
        for band in ("top10", "top20", "top11_20"):
            left = group["{}_{}_mean_r".format(BASELINE, band)].mean()
            right = group["{}_{}_mean_r".format(CANDIDATE, band)].mean()
            yearly.append({"year": int(year), "band": band,
                           "baseline_mean_daily_r": float(left),
                           "candidate_mean_daily_r": float(right),
                           "uplift": float(right-left)})
    pd.DataFrame(yearly).to_csv(
        output/"fixed_count_yearly.csv", index=False)
    (output/"fixed_count_bootstrap.json").write_text(json.dumps(
        bootstrap, ensure_ascii=False, indent=2,
        allow_nan=False)+"\n", encoding="utf-8")
    return summary, daily, bootstrap


def _closed_trade_frame(account, arm, panel, regimes):
    dispositions = pd.read_csv(account/"lot_dispositions.csv")
    trades = pd.read_csv(account/"logical_trades.csv")
    grouped = dispositions.groupby("trade_id", sort=False).agg(
        realized_pnl_cash=("realized_pnl_cash", "sum"),
        exit_date=("fill_date", "max"),
        exit_reason=("exit_reason", "last"),
    ).reset_index()
    selected = trades[[
        "trade_id", "symbol", "opened_at", "closed_at",
        "initial_r_cash_frozen"]].copy()
    selected = selected.dropna(subset=["opened_at"])
    result = selected.merge(grouped, on="trade_id", how="inner",
                            validate="one_to_one")
    result["arm"] = arm
    result["entry_date"] = result.opened_at.astype(int)
    result["exit_date"] = result.exit_date.astype(int)
    result["entry_year"] = result.entry_date//10000
    result["exit_year"] = result.exit_date//10000
    result["r_multiple"] = (result.realized_pnl_cash/
                            result.initial_r_cash_frozen.replace(0, np.nan))
    positions = {int(value): index for index, value in enumerate(panel.dates)}
    result["holding_sessions"] = [
        positions.get(exit_date, 0)-positions.get(entry_date, 0)+1
        for entry_date, exit_date in zip(result.entry_date, result.exit_date)]
    result["holding_bucket"] = pd.cut(
        result.holding_sessions, bins=[-np.inf, 10, 20, 60, np.inf],
        labels=["01_10", "11_20", "21_60", "61_plus"])
    industries, industry_names = [], []
    labels = panel.industry_labels
    for date, symbol in zip(result.entry_date, result.symbol):
        day, column = positions.get(int(date)), panel.symbol_index.get(str(symbol))
        code = int(panel.industry[day, column]) if day is not None and \
            column is not None else -1
        industries.append(code)
        industry_names.append(str(labels.get(code, "UNKNOWN")))
    result["industry_code"] = industries
    result["industry_name"] = industry_names
    result = result.merge(regimes[["date", "market_state"]],
                          left_on="entry_date", right_on="date", how="left")
    result["market_state"] = result.market_state.fillna("UNKNOWN")
    return result.drop(columns=["date"])


def weak_year_attribution(root, output, signal_dir, research_dir):
    yearly = pd.read_csv(root/"yearly_metrics.csv")
    weak_years = yearly.loc[yearly.uplift_pp < 0, "year"].astype(int).tolist()
    panel = SelectionPanelV2.from_research_data(
        signal_dir, research_dir, start_date=20110101, end_date=20260930)
    regimes = classify_market_regimes(panel)
    trade_frames, daily_frames = [], []
    for arm in ARMS:
        account = root/(arm+"_25bp")
        trade_frames.append(_closed_trade_frame(
            account, arm, panel, regimes))
        curve = pd.read_csv(account/"daily_nav.csv")
        daily, _ = attribute_daily_pnl(curve, regimes)
        daily["arm"] = arm
        daily["year"] = daily.date//10000
        daily_frames.append(daily)
    trades = pd.concat(trade_frames, ignore_index=True)
    daily = pd.concat(daily_frames, ignore_index=True)
    trades.to_csv(output/"closed_trade_attribution.csv", index=False)
    daily.to_csv(output/"daily_regime_attribution.csv", index=False)
    weak_trades = trades[trades.exit_year.isin(weak_years)]
    summaries = {}
    for dimension in ("exit_reason", "holding_bucket", "industry_name",
                      "market_state"):
        summary = weak_trades.groupby(
            ["arm", "exit_year", dimension], observed=True, dropna=False).agg(
                trades=("trade_id", "size"),
                realized_pnl_cash=("realized_pnl_cash", "sum"),
                mean_r=("r_multiple", "mean"),
                median_r=("r_multiple", "median")).reset_index()
        summary.to_csv(output/("weak_year_by_"+dimension+".csv"), index=False)
        summaries[dimension] = summary
    state = daily[daily.year.isin(weak_years)].groupby(
        ["arm", "year", "market_state"], dropna=False).agg(
            sessions=("date", "size"), daily_pnl=("daily_pnl", "sum"),
            mean_daily_return=("daily_return", "mean")).reset_index()
    state.to_csv(output/"weak_year_daily_state_summary.csv", index=False)
    return weak_years, summaries, state


def _write_report(output, fixed, bootstrap, weak_years, summaries):
    lines = [
        "# Alpha158事件标签Ridge固定持股数与弱势年度诊断", "",
        "- 状态：`COMPLETE_DIAGNOSTIC_ONLY`",
        "- 固定数量选择先发生，缺失未来标签不会由后续股票补位。",
        "", "## 固定持股数", "",
        "| 分层 | 对照日均R | 候选日均R | 增量 | 对照胜率 | 候选胜率 | 选择重合率 |", "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in fixed.itertuples():
        lines.append("| {} | {:.4f} | {:.4f} | {:+.4f} | {:.2%} | {:.2%} | {:.2%} |".format(
            row.band, row.baseline_mean_daily_r, row.candidate_mean_daily_r,
            row.mean_daily_r_uplift, row.baseline_mean_win_rate,
            row.candidate_mean_win_rate, row.mean_selection_overlap))
    lines += ["", "20日区块Bootstrap的日均R增量区间：", ""]
    for band in ("top10", "top20", "top11_20"):
        item = bootstrap[band]["20"]
        lines.append("- `{}`：`[{:+.4f}, {:+.4f}]`，中位数 `{:+.4f}`。".format(
            band, item["lower"], item["upper"], item["median"]))
    lines += ["", "## 弱势年度", "",
              "账户增量为负的年度片段：`{}`。".format(
                  ", ".join(str(item) for item in weak_years)), ""]
    reason = summaries["exit_reason"]
    candidate = reason[reason.arm.eq(CANDIDATE)].groupby(
        "exit_reason").realized_pnl_cash.sum().sort_values()
    lines.append("候选在这些年度按退出原因汇总的已实现盈亏：")
    lines.append("")
    for name, value in candidate.items():
        lines.append("- `{}`：`{:+,.0f}` 元。".format(name, value))
    lines += [
        "", "固定数量结果只检验排序质量；弱势年度交易归因采用已实现交易，",
        "每日市场状态归因另行保存，二者不得混为账户年度收益。", "",
    ]
    (output/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    output = args.root/"fixed_count_and_weak_year_diagnostics"
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    fixed, _, bootstrap = fixed_count_analysis(
        args.root/"oos_predictions.csv.gz", output)
    gc.collect()
    weak_years, summaries, _ = weak_year_attribution(
        args.root, output, args.signal_dir, args.research_dir)
    _write_report(output, fixed, bootstrap, weak_years, summaries)
    (output/"completion.json").write_text(json.dumps({
        "status": "COMPLETE_DIAGNOSTIC_ONLY", "weak_years": weak_years,
        "automatic_strategy_change": False}, ensure_ascii=False,
        indent=2)+"\n", encoding="utf-8")
    print((output/"REPORT.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Reprice frozen ML account fills without changing any trading decision."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_BACKTEST = Path(
    "/Users/wjy/abu/backtests/alpha158_all_mean_ml_combiner_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_ml_fixed_path_repricing_v1_20261007")
ARMS = ("all_mean_rank_a0", "all_mean_rank_a1")
SOURCE_COST_BPS = 25.0
TARGET_COST_BPS = (25.0, 40.0, 60.0)
EXECUTION = {
    "broker_rate": 0.00025,
    "min_commission": 5.0,
    "transfer_rate": 0.00001,
    "sell_stamp_rate": 0.0005,
}


def _fees(quantity, price, side, execution):
    gross = np.asarray(quantity, dtype=float) * np.asarray(price, dtype=float)
    commission = np.maximum(
        gross * float(execution["broker_rate"]),
        float(execution["min_commission"]))
    transfer = gross * float(execution["transfer_rate"])
    stamp = np.where(
        np.asarray(side) == "sell",
        gross * float(execution["sell_stamp_rate"]), 0.0)
    return commission, transfer, stamp


def validate_source_fees(fills, execution, tolerance=1e-8):
    filled = fills[fills.status.eq("filled")]
    commission, transfer, stamp = _fees(
        filled.quantity, filled.fill_price_raw, filled.side, execution)
    errors = {
        "commission": float(np.max(np.abs(commission-filled.commission))),
        "transfer_fee": float(np.max(np.abs(transfer-filled.transfer_fee))),
        "stamp_tax": float(np.max(np.abs(stamp-filled.stamp_tax))),
    }
    if max(errors.values()) > tolerance:
        raise ValueError("source fee contract does not reproduce stored fills")
    return errors


def account_metrics(capital):
    values = np.asarray(capital, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all() or np.any(values <= 0):
        raise ValueError("capital curve must be finite, positive and non-trivial")
    daily = pd.Series(values).pct_change().fillna(0.0).to_numpy()
    running_peak = np.maximum.accumulate(values)
    drawdown = values/running_peak-1.0
    tail_count = max(1, int(np.ceil(0.05*len(daily))))
    cumulative = values[-1]/values[0]-1.0
    annualized = (values[-1]/values[0])**(252/len(values))-1.0
    return {
        "return_pct": float(cumulative*100),
        "annualized_return_pct": float(annualized*100),
        "max_drawdown_pct": float(drawdown.min()*100),
        "daily_expected_shortfall_95_pct": float(
            np.sort(daily)[:tail_count].mean()*100),
    }


def reprice_frozen_path(curve, fills, source_bps, target_bps, execution):
    """Reprice fills incrementally while preserving source quantities and dates."""
    if curve.date.duplicated().any():
        raise ValueError("source curve contains duplicate dates")
    filled = fills[fills.status.eq("filled")].copy()
    missing_dates = set(filled.date)-set(curve.date)
    if missing_dates:
        raise ValueError("fill dates are absent from source curve")
    direction = np.where(filled.side.eq("buy"), 1.0, -1.0)
    delta_bps = float(target_bps)-float(source_bps)
    target_price = (
        filled.fill_price_raw.to_numpy(dtype=float) +
        direction*filled.reference_price.to_numpy(dtype=float)*delta_bps/10000)
    if np.any(target_price <= 0):
        raise ValueError("repriced fill is non-positive")
    quantity = filled.quantity.to_numpy(dtype=float)
    source_gross = quantity*filled.fill_price_raw.to_numpy(dtype=float)
    target_gross = quantity*target_price
    target_commission, target_transfer, target_stamp = _fees(
        quantity, target_price, filled.side.to_numpy(), execution)
    source_fees = filled[["commission", "transfer_fee", "stamp_tax"]].sum(
        axis=1).to_numpy(dtype=float)
    target_fees = target_commission+target_transfer+target_stamp
    source_cash_flow = np.where(
        filled.side.eq("buy"), -(source_gross+source_fees),
        source_gross-source_fees)
    target_cash_flow = np.where(
        filled.side.eq("buy"), -(target_gross+target_fees),
        target_gross-target_fees)
    filled["cash_delta"] = target_cash_flow-source_cash_flow
    by_date = filled.groupby("date", sort=True).cash_delta.sum()
    result = curve[["date", "capital"]].copy()
    result.rename(columns={"capital": "source_capital"}, inplace=True)
    result["daily_cash_delta"] = result.date.map(by_date).fillna(0.0)
    result["cumulative_cash_delta"] = result.daily_cash_delta.cumsum()
    result["repriced_capital"] = (
        result.source_capital+result.cumulative_cash_delta)
    result["repriced_daily_return"] = (
        result.repriced_capital.pct_change().fillna(0.0))
    metrics = account_metrics(result.repriced_capital)
    metrics.update({
        "source_bps": float(source_bps),
        "target_bps": float(target_bps),
        "frozen_fill_count": int(len(filled)),
        "terminal_cash_delta": float(result.cumulative_cash_delta.iloc[-1]),
        "source_reproduction_max_abs_error": (
            float(np.max(np.abs(result.repriced_capital-result.source_capital)))
            if float(target_bps) == float(source_bps) else None),
    })
    return result, metrics


def run(backtest, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("refusing to overwrite fixed-path repricing")
    output.mkdir(parents=True)
    summaries = []
    fee_audits = {}
    for arm in ARMS:
        source = Path(backtest)/"{}_25bp".format(arm)
        curve = pd.read_csv(source/"daily_nav.csv")
        fills = pd.read_csv(source/"fills.csv", dtype={"symbol": str})
        fee_audits[arm] = validate_source_fees(fills, EXECUTION)
        for target in TARGET_COST_BPS:
            repriced, metrics = reprice_frozen_path(
                curve, fills, SOURCE_COST_BPS, target, EXECUTION)
            metrics["arm"] = arm
            summaries.append(metrics)
            directory = output/"{}_25_to_{}bp".format(arm, int(target))
            directory.mkdir()
            repriced.to_csv(directory/"daily_nav.csv", index=False)
    summary = pd.DataFrame(summaries).sort_values(
        ["arm", "target_bps"], kind="mergesort").reset_index(drop=True)
    for arm, frame in summary.groupby("arm", sort=False):
        ordered = frame.sort_values("target_bps")
        if not ordered.return_pct.is_monotonic_decreasing:
            raise ValueError("fixed-path return must decline as cost increases")
    summary.to_csv(output/"summary.csv", index=False)
    payload = {
        "analysis_id": "alpha158_ml_fixed_path_repricing_v1",
        "source_backtest": str(Path(backtest)),
        "source_cost_bps": SOURCE_COST_BPS,
        "target_cost_bps": list(TARGET_COST_BPS),
        "fee_contract": EXECUTION,
        "fee_audits": fee_audits,
        "decision": "DIAGNOSTIC_ONLY_NO_STRATEGY_CHANGE",
        "method": (
            "Freeze the 25bp quantities, fill dates, symbols and exits. Reprice "
            "each stored fill by the incremental target-minus-source slippage, "
            "recompute fees, and add cumulative cash-flow deltas to source NAV."),
    }
    (output/"report.json").write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False)+"\n", encoding="utf-8")
    _write_markdown(output, summary, payload)
    return summary


def _write_markdown(output, summary, payload):
    lines = [
        "# Alpha158 ML 冻结订单路径重定价", "",
        "本诊断冻结 25bp 账户的证券、数量、成交日和退出日，只改变成交滑点与由成交金额产生的费用。"
        "因此它回答的是“同一份交易清单被额外成本侵蚀多少”，不是更高成本下策略会重新做出什么决策。", "",
        "| 账户 | 目标滑点 | 累计收益 | 年化收益 | 最大回撤 | ES95 | 期末现金影响 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples():
        lines.append(
            "| {arm} | {bps:.0f}bp | {ret:.2f}% | {ann:.2f}% | {dd:.2f}% | "
            "{es:.3f}% | {cash:,.0f} |".format(
                arm=row.arm, bps=row.target_bps, ret=row.return_pct,
                ann=row.annualized_return_pct, dd=row.max_drawdown_pct,
                es=row.daily_expected_shortfall_95_pct,
                cash=row.terminal_cash_delta))
    lines += ["", "## 解读", ""]
    pivot = summary.pivot(index="target_bps", columns="arm", values="return_pct")
    for bps in TARGET_COST_BPS:
        uplift = pivot.loc[bps, "all_mean_rank_a1"]-pivot.loc[
            bps, "all_mean_rank_a0"]
        lines.append(
            "- {bps:.0f}bp 固定路径下，A1 相对 A0 累计收益增量为 `{uplift:+.2f}pp`。".format(
                bps=bps, uplift=uplift))
    lines += [
        "", "该诊断不改变 M8 的历史拒绝结论，也不用于事后选择有利成本档。", "",
    ]
    (output/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-dir", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary = run(args.backtest_dir, args.output_dir)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()

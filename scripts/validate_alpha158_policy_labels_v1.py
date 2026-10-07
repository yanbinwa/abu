#!/usr/bin/env python3
"""Validate policy-aligned labels against corrected realised account paths."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config
from abupy.AlphaBu.ABuAlpha158PolicyLabels import (
    Alpha158PolicyLabelBuilder, load_alpha158_policy_label_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2


DEFAULT_BACKTEST = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_mean_ml_combiner_v1_stop_invalidated_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_policy_labels_v1_parity_20261007")
ARMS = ("all_mean_rank_a0_25bp", "all_mean_rank_a1_25bp")
EVENT_REASONS = {"INITIAL_STOP", "TRAILING_STOP", "STAGNATION"}


def _spearman(left, right):
    frame = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(frame) < 3:
        return None
    value = frame.rank(method="average").left.corr(
        frame.rank(method="average").right)
    return None if not np.isfinite(value) else float(value)


def summarize_parity(frame):
    entry_match = frame.entry_executable & frame.entry_price_match
    observed = frame.label_reason.isin(EVENT_REASONS)
    reason_match = frame.label_reason.eq(frame.actual_reason)
    date_match = frame.label_exit_date.eq(frame.actual_exit_date)
    censored = ~observed
    censored_rows = frame[censored]
    return {
        "evaluated_trades": int(len(frame)),
        "entry_executable_rate": float(frame.entry_executable.mean()),
        "entry_price_match_rate": float(frame.entry_price_match.mean()),
        "entry_price_max_abs_error": float(frame.entry_price_abs_error.max()),
        "observed_event_trades": int(observed.sum()),
        "censored_or_missing_trades": int(censored.sum()),
        "observed_reason_match_rate": float(reason_match[observed].mean()),
        "observed_exit_date_match_rate": float(date_match[observed].mean()),
        "all_reason_match_rate": float(reason_match.mean()),
        "all_exit_date_match_rate": float(date_match.mean()),
        "censored_realized_pnl_cash": float(
            frame.loc[censored, "realized_pnl_cash"].sum()),
        "censored_actual_holding_sessions_mean": float(
            censored_rows.actual_holding_sessions.mean()),
        "censored_label_r_mean": float(censored_rows.label_event_r.mean()),
        "censored_actual_r_mean": float(censored_rows.actual_realized_r.mean()),
        "censored_label_actual_r_spearman": _spearman(
            censored_rows.label_event_r, censored_rows.actual_realized_r),
        "censored_r_sign_match_rate": float((
            np.sign(censored_rows.label_event_r) ==
            np.sign(censored_rows.actual_realized_r)).mean()),
        "total_realized_pnl_cash": float(frame.realized_pnl_cash.sum()),
        "entry_contract_passed": bool(entry_match.all()),
        "observed_event_contract_passed": bool(
            reason_match[observed].all() and date_match[observed].all()),
    }


def account_parity(account_dir, panel, builder):
    account_dir = Path(account_dir)
    trades = pd.read_csv(
        account_dir/"logical_trades.csv", dtype={"symbol": str})
    trades = trades[trades.status.eq("CLOSED")].copy()
    orders = pd.read_csv(account_dir/"orders.csv", dtype={"symbol": str})
    entries = orders[orders.side.eq("buy")][
        ["intent_id", "created_asof"]].copy()
    fills = pd.read_csv(account_dir/"fills.csv", dtype={"symbol": str})
    buy_fills = fills[(fills.side.eq("buy")) & (fills.status.eq("filled"))][
        ["intent_id", "fill_price_raw"]].copy()
    disposition = pd.read_csv(
        account_dir/"lot_dispositions.csv", dtype={"symbol": str})
    disposition = disposition.groupby("trade_id", sort=False).agg(
        actual_reason=("exit_reason", "first"),
        actual_exit_date=("fill_date", "max"),
        realized_pnl_cash=("realized_pnl_cash", "sum"))
    lots = pd.read_csv(account_dir/"position_lots.csv", dtype={"symbol": str})
    risk = lots.groupby("trade_id", sort=False).risk_cash_frozen.sum().rename(
        "risk_cash_frozen")
    merged = trades.merge(
        entries, left_on="entry_intent_id", right_on="intent_id",
        validate="one_to_one").merge(
            buy_fills, left_on="entry_intent_id", right_on="intent_id",
            validate="one_to_one", suffixes=("", "_fill")).merge(
                disposition, left_on="trade_id", right_index=True,
                validate="one_to_one").merge(
                    risk, left_on="trade_id", right_index=True,
                    validate="one_to_one")
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    rows = []
    for trade in merged.itertuples():
        day = date_index[int(trade.created_asof)]
        if day+61 >= len(panel.dates):
            continue
        column = panel.symbol_index[str(trade.symbol)]
        label = builder.build_day(day, [column]).iloc[0]
        entry_error = (
            abs(float(label.entry_fill_raw)-float(trade.fill_price_raw))
            if label.entry_executable else np.nan)
        rows.append({
            "trade_id": trade.trade_id, "symbol": trade.symbol,
            "signal_asof": int(trade.created_asof),
            "entry_executable": bool(label.entry_executable),
            "entry_price_abs_error": entry_error,
            "entry_price_match": bool(
                np.isfinite(entry_error) and entry_error <= 1e-10),
            "label_reason": label.event_path_reason_60d,
            "label_event_r": label.event_path_r_60d,
            "actual_reason": trade.actual_reason,
            "label_exit_date": label.event_path_end_date_60d,
            "actual_exit_date": int(trade.actual_exit_date),
            "realized_pnl_cash": float(trade.realized_pnl_cash),
            "actual_realized_r": (
                float(trade.realized_pnl_cash/trade.risk_cash_frozen)
                if trade.risk_cash_frozen > 0 else np.nan),
            "actual_holding_sessions": int(
                date_index[int(trade.actual_exit_date)]-(day+1)+1),
        })
    return pd.DataFrame(rows)


def run(args):
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20110101, end_date=20260930)
    strategy = load_alpha158_lite_config(args.strategy_config)
    label_config = load_alpha158_policy_label_config(args.label_config)
    builder = Alpha158PolicyLabelBuilder(panel, strategy, label_config)
    args.output_dir.mkdir(parents=True)
    summaries = {}
    for arm in ARMS:
        frame = account_parity(args.backtest_dir/arm, panel, builder)
        frame.to_csv(args.output_dir/"{}_parity.csv".format(arm), index=False)
        summaries[arm] = summarize_parity(frame)
    passed = all(
        item["entry_contract_passed"] and
        item["observed_event_contract_passed"]
        for item in summaries.values())
    payload = {
        "validation_id": "alpha158_policy_labels_v1_parity",
        "status": "PASS_PARITY_WITH_60D_CENSORING" if passed else "FAIL",
        "label_version": label_config.label_version,
        "label_config_sha256": builder.config_sha256,
        "source_backtest": str(args.backtest_dir),
        "arms": summaries,
        "censoring_note": (
            "TIME_MARK_60 and missing terminal marks are deliberate horizon "
            "censoring, not claimed as matches to later realised exits."),
    }
    (args.output_dir/"report.json").write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False)+"\n", encoding="utf-8")
    _write_markdown(args.output_dir, payload)
    return payload


def _write_markdown(output, payload):
    lines = [
        "# Alpha158事件退出标签对账", "",
        "| 账户 | 评价交易 | 入场价一致 | 60日内事件 | 事件原因一致 | 退出日一致 | 60日截尾 | 截尾交易已实现盈亏 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, row in payload["arms"].items():
        lines.append(
            "| {arm} | {total} | {entry:.1%} | {events} | {reason:.1%} | "
            "{date:.1%} | {censored} | {pnl:,.0f} |".format(
                arm=arm, total=row["evaluated_trades"],
                entry=row["entry_price_match_rate"],
                events=row["observed_event_trades"],
                reason=row["observed_reason_match_rate"],
                date=row["observed_exit_date_match_rate"],
                censored=row["censored_or_missing_trades"],
                pnl=row["censored_realized_pnl_cash"]))
    lines += [
        "", "## 60日截尾诊断", "",
        "| 账户 | 最终平均持有日 | 60日标记平均R | 最终平均R | R相关 | 盈亏符号一致 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm, row in payload["arms"].items():
        lines.append(
            "| {arm} | {held:.1f} | {label:.3f} | {actual:.3f} | "
            "{corr:.3f} | {sign:.1%} |".format(
                arm=arm,
                held=row["censored_actual_holding_sessions_mean"],
                label=row["censored_label_r_mean"],
                actual=row["censored_actual_r_mean"],
                corr=row["censored_label_actual_r_spearman"] or 0.0,
                sign=row["censored_r_sign_match_rate"]))
    lines += [
        "", "结论：`{}`。60日内已经发生的事件退出必须逐笔一致；超过60日的后续真实退出只用于衡量截尾影响，不冒充标签对账成功。".format(
            payload["status"]), "",
    ]
    (output/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-dir", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    parser.add_argument("--strategy-config", type=Path,
                        default=ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--label-config", type=Path,
                        default=ROOT/"configs/selection/alpha158_policy_labels_v1.json")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

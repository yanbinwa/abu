#!/usr/bin/env python3
"""Diagnose ML scores against realised event-exit trade outcomes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_BACKTEST = Path(
    "/Users/wjy/abu/backtests/alpha158_all_mean_ml_combiner_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_ml_policy_outcomes_v1_20261007")
ARMS = ("all_mean_rank_a0_25bp", "all_mean_rank_a1_25bp")
SCORE_COLUMNS = ("a0_score", "a1_score", "score_delta")


def _spearman(left, right):
    frame = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(frame) < 3:
        return None
    ranked = frame.rank(method="average")
    value = ranked.left.corr(ranked.right)
    return None if not np.isfinite(value) else float(value)


def allocate_cash_events(trades, position_events):
    """Allocate symbol cash events to the sole trade open on the event date."""
    if position_events.empty:
        return pd.Series(dtype=float, name="cash_event_pnl")
    events = position_events.loc[
        pd.to_numeric(position_events.cash_delta, errors="coerce").fillna(0) != 0,
        ["date", "symbol", "cash_delta"],
    ].copy()
    if events.empty:
        return pd.Series(dtype=float, name="cash_event_pnl")
    candidates = events.merge(
        trades[["trade_id", "symbol", "opened_at", "closed_at"]],
        on="symbol", how="left", validate="many_to_many")
    candidates = candidates[
        candidates.date.ge(candidates.opened_at)
        & candidates.date.le(candidates.closed_at)]
    counts = candidates.groupby(["date", "symbol"], sort=False).trade_id.nunique()
    ambiguous = counts[counts.ne(1)]
    if len(ambiguous):
        raise ValueError("cash event maps to multiple concurrently open trades")
    return candidates.groupby("trade_id", sort=False).cash_delta.sum().rename(
        "cash_event_pnl")


def build_trade_outcomes(account_dir, predictions):
    account_dir = Path(account_dir)
    trades = pd.read_csv(
        account_dir/"logical_trades.csv", dtype={"symbol": str})
    trades = trades[trades.status.eq("CLOSED")].copy()
    lots = pd.read_csv(account_dir/"position_lots.csv", dtype={"symbol": str})
    dispositions = pd.read_csv(
        account_dir/"lot_dispositions.csv", dtype={"symbol": str})
    orders = pd.read_csv(account_dir/"orders.csv", dtype={"symbol": str})
    events = pd.read_csv(
        account_dir/"position_events.csv", dtype={"symbol": str})

    risk = lots.groupby("trade_id", sort=False).risk_cash_frozen.sum().rename(
        "risk_cash_frozen")
    realised = dispositions.groupby("trade_id", sort=False).agg(
        disposition_pnl_cash=("realized_pnl_cash", "sum"),
        exit_reason=("exit_reason", lambda value: (
            value.iloc[0] if value.nunique(dropna=False) == 1 else "MULTIPLE")),
        exit_fill_date=("fill_date", "max"),
    )
    cash_events = allocate_cash_events(trades, events)
    entry_orders = orders[orders.side.eq("buy")][
        ["intent_id", "created_asof"]].copy()
    if entry_orders.intent_id.duplicated().any():
        raise ValueError("entry intent has multiple buy orders")

    result = trades[
        ["trade_id", "symbol", "entry_intent_id", "opened_at", "closed_at"]
    ].merge(risk, left_on="trade_id", right_index=True, how="left")
    result = result.merge(
        realised, left_on="trade_id", right_index=True, how="left",
        validate="one_to_one")
    result = result.merge(
        cash_events, left_on="trade_id", right_index=True, how="left",
        validate="one_to_one")
    result.cash_event_pnl = result.cash_event_pnl.fillna(0.0)
    result["realized_pnl_cash"] = (
        result.disposition_pnl_cash+result.cash_event_pnl)
    result = result.merge(
        entry_orders, left_on="entry_intent_id", right_on="intent_id",
        how="left", validate="one_to_one")
    result.drop(columns=["intent_id"], inplace=True)
    score_frame = predictions.copy()
    score_frame["score_delta"] = score_frame.a1_score-score_frame.a0_score
    result = result.merge(
        score_frame[["signal_asof", "symbol", *SCORE_COLUMNS]],
        left_on=["created_asof", "symbol"],
        right_on=["signal_asof", "symbol"], how="left", validate="one_to_one")
    result["valid_r"] = (
        result.risk_cash_frozen.gt(0)
        & np.isfinite(result.risk_cash_frozen)
        & np.isfinite(result.realized_pnl_cash))
    result["realized_r"] = np.where(
        result.valid_r,
        result.realized_pnl_cash/result.risk_cash_frozen,
        np.nan)
    return result


def score_diagnostics(outcomes):
    valid = outcomes[outcomes.valid_r].copy()
    summary = {
        "closed_trades": int(len(outcomes)),
        "valid_r_trades": int(len(valid)),
        "invalid_r_trades": int((~outcomes.valid_r).sum()),
        "cash_event_counted_trades": int(valid.cash_event_pnl.ne(0).sum()),
        "mean_realized_r": float(valid.realized_r.mean()),
        "median_realized_r": float(valid.realized_r.median()),
        "win_rate": float(valid.realized_r.gt(0).mean()),
        "total_realized_pnl_cash": float(valid.realized_pnl_cash.sum()),
        "total_risk_cash": float(valid.risk_cash_frozen.sum()),
        "aggregate_realized_pnl_over_risk": float(
            valid.realized_pnl_cash.sum()/valid.risk_cash_frozen.sum()),
        "total_cash_event_pnl": float(valid.cash_event_pnl.sum()),
        "score_spearman": {},
        "score_quintiles": {},
        "exit_reason": {},
    }
    for column in SCORE_COLUMNS:
        summary["score_spearman"][column] = _spearman(
            valid[column], valid.realized_r)
        ranked = valid[column].rank(method="first")
        bins = pd.qcut(ranked, q=5, labels=False, duplicates="drop")+1
        table = valid.assign(quintile=bins).groupby("quintile").agg(
            trades=("trade_id", "size"),
            mean_realized_r=("realized_r", "mean"),
            median_realized_r=("realized_r", "median"),
            win_rate=("realized_r", lambda value: value.gt(0).mean()),
        )
        summary["score_quintiles"][column] = {
            str(int(index)): {
                key: (int(row[key]) if key == "trades" else float(row[key]))
                for key in table.columns}
            for index, row in table.iterrows()}
    exits = valid.groupby("exit_reason").agg(
        trades=("trade_id", "size"),
        mean_realized_r=("realized_r", "mean"),
        median_realized_r=("realized_r", "median"),
        win_rate=("realized_r", lambda value: value.gt(0).mean()),
    )
    summary["exit_reason"] = {
        str(index): {
            key: (int(row[key]) if key == "trades" else float(row[key]))
            for key in exits.columns}
        for index, row in exits.iterrows()}
    return summary


def compare_arms(left, right):
    """Describe common and substituted selections without claiming causality."""
    left = left[left.valid_r].copy()
    right = right[right.valid_r].copy()
    key_columns = ["signal_asof", "symbol"]
    left_keys = set(map(tuple, left[key_columns].itertuples(index=False, name=None)))
    right_keys = set(map(tuple, right[key_columns].itertuples(index=False, name=None)))

    def summarise(frame, keys):
        mask = [key in keys for key in zip(frame.signal_asof, frame.symbol)]
        selected = frame.loc[mask]
        risk = float(selected.risk_cash_frozen.sum())
        pnl = float(selected.realized_pnl_cash.sum())
        return {
            "trades": int(len(selected)),
            "realized_pnl_cash": pnl,
            "risk_cash": risk,
            "aggregate_realized_pnl_over_risk": pnl/risk if risk > 0 else None,
            "mean_realized_r": float(selected.realized_r.mean()),
            "median_realized_r": float(selected.realized_r.median()),
        }

    common = left_keys & right_keys
    union = left_keys | right_keys
    return {
        "selection_jaccard": len(common)/len(union) if union else None,
        "common_key_count": len(common),
        "left_only_key_count": len(left_keys-common),
        "right_only_key_count": len(right_keys-common),
        "left_common": summarise(left, common),
        "right_common": summarise(right, common),
        "left_only": summarise(left, left_keys-common),
        "right_only": summarise(right, right_keys-common),
    }


def load_prediction_subset(path, keys, chunksize=500_000):
    key_frame = keys[["signal_asof", "symbol"]].drop_duplicates().copy()
    key_frame.signal_asof = key_frame.signal_asof.astype(int)
    wanted = set(map(tuple, key_frame.itertuples(index=False, name=None)))
    found = []
    columns = ["signal_asof", "symbol", "a0_score", "a1_score"]
    for chunk in pd.read_csv(
            path, usecols=columns, dtype={"symbol": str}, chunksize=chunksize):
        mask = [key in wanted for key in zip(chunk.signal_asof, chunk.symbol)]
        if any(mask):
            found.append(chunk.loc[mask])
    result = pd.concat(found, ignore_index=True) if found else pd.DataFrame(
        columns=columns)
    if result[["signal_asof", "symbol"]].duplicated().any():
        raise ValueError("duplicate OOS score key")
    missing = wanted-set(zip(result.signal_asof, result.symbol))
    if missing:
        raise ValueError("missing OOS scores for {} trade keys".format(len(missing)))
    return result


def _entry_keys(account_dir):
    orders = pd.read_csv(
        Path(account_dir)/"orders.csv", dtype={"symbol": str})
    return orders.loc[orders.side.eq("buy"), ["created_asof", "symbol"]].rename(
        columns={"created_asof": "signal_asof"})


def run(backtest, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("refusing to overwrite policy outcome diagnostic")
    keys = pd.concat(
        [_entry_keys(Path(backtest)/arm) for arm in ARMS], ignore_index=True)
    predictions = load_prediction_subset(
        Path(backtest)/"oos_predictions.csv.gz", keys)
    output.mkdir(parents=True)
    summaries = {}
    outcome_frames = {}
    for arm in ARMS:
        outcomes = build_trade_outcomes(Path(backtest)/arm, predictions)
        outcome_frames[arm] = outcomes
        outcomes.to_csv(output/"{}_trade_outcomes.csv".format(arm), index=False)
        summaries[arm] = score_diagnostics(outcomes)
    payload = {
        "analysis_id": "alpha158_ml_policy_outcomes_v1",
        "source_backtest": str(Path(backtest)),
        "decision": "DIAGNOSTIC_ONLY_NO_STRATEGY_CHANGE",
        "scope_warning": (
            "Correlations are conditional on each account's selected and closed "
            "trades. They diagnose label-policy alignment but do not estimate an "
            "unbiased cross-sectional training target."),
        "arms": summaries,
        "cross_arm_selection": compare_arms(
            outcome_frames[ARMS[0]], outcome_frames[ARMS[1]]),
    }
    (output/"report.json").write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False)+"\n", encoding="utf-8")
    _write_markdown(output, payload)
    return payload


def _write_markdown(output, payload):
    lines = [
        "# Alpha158 ML 事件退出结果诊断", "",
        "本报告把冻结 OOS 分数与实际已平仓交易对齐，分母使用 lot 级冻结风险，"
        "分子包含卖出已实现盈亏和持有期内现金分红。无效或零风险分母被单独排除。", "",
        "| 账户 | 已平仓 | 有效R | 平均R | 中位R | 胜率 | A0分数相关 | A1分数相关 | A1-A0相关 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, item in payload["arms"].items():
        corr = item["score_spearman"]
        lines.append(
            "| {arm} | {closed} | {valid} | {mean:.3f} | {median:.3f} | "
            "{win:.1%} | {a0:.3f} | {a1:.3f} | {delta:.3f} |".format(
                arm=arm, closed=item["closed_trades"],
                valid=item["valid_r_trades"], mean=item["mean_realized_r"],
                median=item["median_realized_r"], win=item["win_rate"],
                a0=corr["a0_score"] or 0.0, a1=corr["a1_score"] or 0.0,
                delta=corr["score_delta"] or 0.0))
    cross = payload["cross_arm_selection"]
    lines += [
        "", "## 选股替换归因", "",
        "两账户有效交易键的 Jaccard 为 `{:.1%}`；共同交易 {} 笔，A0 独有 {} 笔，A1 独有 {} 笔。".format(
            cross["selection_jaccard"], cross["common_key_count"],
            cross["left_only_key_count"], cross["right_only_key_count"]),
        "",
        "| 分组 | 交易数 | 已实现盈亏 | 冻结风险 | 盈亏/风险 | 平均R | 中位R |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "left_common": "A0共同交易", "right_common": "A1共同交易",
        "left_only": "A0独有交易", "right_only": "A1独有交易",
    }
    for key, label in labels.items():
        row = cross[key]
        lines.append(
            "| {label} | {trades} | {pnl:,.0f} | {risk:,.0f} | {ratio:.2%} | "
            "{mean:.3f} | {median:.3f} |".format(
                label=label, trades=row["trades"],
                pnl=row["realized_pnl_cash"], risk=row["risk_cash"],
                ratio=row["aggregate_realized_pnl_over_risk"] or 0.0,
                mean=row["mean_realized_r"], median=row["median_realized_r"]))
    lines += ["", "## 退出类型", ""]
    for arm, item in payload["arms"].items():
        lines.append("### {}".format(arm))
        lines.append("")
        lines.append("| 退出原因 | 交易数 | 平均R | 中位R | 胜率 |")
        lines.append("|---|---:|---:|---:|---:|")
        for reason, row in item["exit_reason"].items():
            lines.append(
                "| {reason} | {trades} | {mean:.3f} | {median:.3f} | {win:.1%} |".format(
                    reason=reason, trades=row["trades"],
                    mean=row["mean_realized_r"],
                    median=row["median_realized_r"], win=row["win_rate"]))
        lines.append("")
    lines += [
        "## 结论边界", "",
        "这些统计只覆盖各账户实际选中并已平仓的交易，存在选择条件，不能直接作为新标签的无偏收益证明。"
        "它们只用于判断固定20日横截面标签与事件退出结果是否存在明显错配。", "",
    ]
    (output/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-dir", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = run(args.backtest_dir, args.output_dir)
    print(json.dumps(payload["arms"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

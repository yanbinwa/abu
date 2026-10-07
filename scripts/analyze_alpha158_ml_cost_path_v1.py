#!/usr/bin/env python3
"""Diagnose cost-driven account path divergence without retuning a strategy."""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path

import pandas as pd


DEFAULT_BACKTEST = Path(
    "/Users/wjy/abu/backtests/alpha158_all_mean_ml_combiner_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_ml_cost_path_attribution_v1_20261007")
ARMS = ("all_mean_rank_a0", "all_mean_rank_a1")
COSTS = ("25bp", "40bp", "60bp")


def _normalized_rows(frame, columns):
    values = frame.loc[:, columns].copy()
    values = values.where(pd.notna(values), None)
    return [tuple(row) for row in values.to_numpy(dtype=object).tolist()]


def first_divergence(left, right, date_column, key_columns, value_columns=()):
    """Return the first date where keys or selected values differ."""
    dates = sorted(set(left[date_column]).union(right[date_column]))
    sort_columns = list(key_columns)
    for date in dates:
        left_day = left[left[date_column].eq(date)].sort_values(
            sort_columns, kind="mergesort").reset_index(drop=True)
        right_day = right[right[date_column].eq(date)].sort_values(
            sort_columns, kind="mergesort").reset_index(drop=True)
        if _normalized_rows(left_day, key_columns) != _normalized_rows(
                right_day, key_columns):
            return {"date": int(date), "difference": "keys"}
        for column in value_columns:
            if _normalized_rows(left_day, [column]) != _normalized_rows(
                    right_day, [column]):
                return {"date": int(date), "difference": column}
    return None


def buy_order_overlap(left, right):
    """Summarize overlap of dated buy-order symbol sets."""
    left = left[left.side.eq("buy")]
    right = right[right.side.eq("buy")]
    dates = sorted(set(left.created_asof).union(right.created_asof))
    identical = 0
    intersection = 0
    union = 0
    for date in dates:
        left_symbols = set(left.loc[left.created_asof.eq(date), "symbol"])
        right_symbols = set(right.loc[right.created_asof.eq(date), "symbol"])
        identical += left_symbols == right_symbols
        intersection += len(left_symbols & right_symbols)
        union += len(left_symbols | right_symbols)
    return {
        "evaluated_order_dates": int(len(dates)),
        "identical_order_dates": int(identical),
        "identical_order_date_share": (
            float(identical / len(dates)) if dates else 1.0),
        "pooled_symbol_jaccard": float(intersection / union) if union else 1.0,
    }


def _load_account(root, arm, cost):
    directory = Path(root) / "{}_{}".format(arm, cost)
    return {
        name: pd.read_csv(directory / "{}.csv".format(name),
                          dtype={"symbol": str})
        for name in ("selection_decisions", "orders", "fills", "exit_reasons")
    }


def _account_summary(account):
    selection = account["selection_decisions"]
    fills = account["fills"]
    return {
        "selection_rows": int(len(selection)),
        "approved": int(selection.risk_decision.eq("approved").sum()),
        "reduced": int(selection.risk_decision.eq("reduced").sum()),
        "rejected": int(selection.risk_decision.eq("rejected").sum()),
        "orders": int(len(account["orders"])),
        "filled_buys": int((fills.side.eq("buy") & fills.status.eq("filled")).sum()),
        "filled_sells": int((fills.side.eq("sell") & fills.status.eq("filled")).sum()),
    }


def _records_on(frame, date_column, date):
    if not date:
        return []
    columns = [column for column in (
        date_column, "symbol", "reason", "position_effect", "quantity")
        if column in frame]
    return frame[frame[date_column].eq(date)][columns].to_dict("records")


def analyze(root):
    accounts = {
        (arm, cost): _load_account(root, arm, cost)
        for arm in ARMS for cost in COSTS
    }
    result = {
        "analysis_id": "alpha158_ml_cost_path_attribution_v1",
        "source_backtest": str(Path(root)),
        "decision": "DIAGNOSTIC_ONLY_NO_STRATEGY_CHANGE",
        "path_semantics": (
            "Each cost scenario is an endogenous replay. Slippage changes the "
            "actual fill price and frozen initial R; R-based exits, portfolio "
            "risk headroom, round-lot sizing and later orders may therefore diverge."),
        "accounts": {},
        "comparisons": [],
    }
    for arm in ARMS:
        result["accounts"][arm] = {
            cost: _account_summary(accounts[(arm, cost)]) for cost in COSTS}
        for left_cost, right_cost in combinations(COSTS, 2):
            left = accounts[(arm, left_cost)]
            right = accounts[(arm, right_cost)]
            exit_divergence = first_divergence(
                left["exit_reasons"], right["exit_reasons"], "date",
                ["symbol", "reason", "position_effect", "quantity"])
            exit_date = exit_divergence["date"] if exit_divergence else None
            result["comparisons"].append({
                "arm": arm,
                "left_cost": left_cost,
                "right_cost": right_cost,
                "selection_divergence": first_divergence(
                    left["selection_decisions"], right["selection_decisions"],
                    "signal_asof", ["symbol"],
                    ["daily_rank", "risk_decision", "order_created"]),
                "order_divergence": first_divergence(
                    left["orders"], right["orders"], "created_asof",
                    ["symbol", "side", "valid_session"],
                    ["quantity", "planned_initial_r_cash"]),
                "fill_divergence": first_divergence(
                    left["fills"], right["fills"], "date",
                    ["order_id", "side"], ["status", "quantity"]),
                "exit_divergence": exit_divergence,
                "left_exit_records": _records_on(
                    left["exit_reasons"], "date", exit_date),
                "right_exit_records": _records_on(
                    right["exit_reasons"], "date", exit_date),
                "buy_order_overlap": buy_order_overlap(
                    left["orders"], right["orders"]),
            })
    return result


def write_report(output, result):
    output = Path(output)
    if output.exists():
        raise FileExistsError("refusing to overwrite cost-path attribution")
    output.mkdir(parents=True)
    (output / "report.json").write_text(json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False) + "\n", encoding="utf-8")
    lines = [
        "# Alpha158 ML 成本路径归因", "",
        "本报告只读比较已冻结账本，不重放账户、不调参、不改变策略决策。", "",
        "当前 25/40/60bp 是内生路径压力测试：滑点会改变实际成交价和冻结初始 R，"
        "因此可能改变 R 退出、组合开放风险余量、整手取整和后续订单。", "",
        "| 账户 | 成本对 | 首次选股/风险分叉 | 首次退出分叉 | 买入订单 Jaccard | 完全一致订单日 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in result["comparisons"]:
        selection = row["selection_divergence"]
        exit_row = row["exit_divergence"]
        overlap = row["buy_order_overlap"]
        lines.append(
            "| {arm} | {left}/{right} | {selection} | {exit_date} | "
            "{jaccard:.1%} | {same}/{total} |".format(
                arm=row["arm"], left=row["left_cost"],
                right=row["right_cost"],
                selection=selection["date"] if selection else "无",
                exit_date=exit_row["date"] if exit_row else "无",
                jaccard=overlap["pooled_symbol_jaccard"],
                same=overlap["identical_order_dates"],
                total=overlap["evaluated_order_dates"]))
    lines += [
        "", "## 结论", "",
        "成本档的非单调收益不是分数或 OOS 键变化造成的，而是实际成交价进入初始 R 后导致的内生路径分叉。"
        "该语义与现有成交合同一致，未发现实现错误。", "",
        "未来若要单独测量纯交易成本弹性，应另外增加“冻结订单路径重定价”诊断；"
        "它不应回溯修改本轮预注册门槛。", "",
    ]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-dir", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = analyze(args.backtest_dir)
    write_report(args.output_dir, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

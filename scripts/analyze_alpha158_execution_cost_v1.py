#!/usr/bin/env python3
"""Attribute execution friction and small-order outcomes without tuning a filter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_execution_cost_attribution_v1.json"
DEFAULT_OUTPUT = Path("/Users/wjy/abu/backtests/alpha158_execution_cost_attribution_v1_20261006")


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def _pct(value):
    return f"{value:.3f}%"


def analyze(config, output):
    source = Path(config["source_backtest"])
    if output.exists():
        raise FileExistsError("refusing to overwrite execution attribution")
    output.mkdir(parents=True)
    fills = pd.read_csv(source / "fills.csv")
    orders = pd.read_csv(source / "orders.csv")
    lots = pd.read_csv(source / "position_lots.csv")
    disp = pd.read_csv(source / "lot_dispositions.csv")
    metrics = json.loads((source / "metrics.json").read_text(encoding="utf-8"))
    filled = fills[fills.status.eq("filled")].copy()
    filled["notional"] = filled.quantity * filled.fill_price_raw
    filled["proportional_commission"] = (
        filled.notional * float(config["broker_rate"]))
    filled["minimum_commission_excess"] = (
        filled.commission - filled.proportional_commission).clip(lower=0)
    filled["minimum_commission_hit"] = np.isclose(
        filled.commission, float(config["minimum_commission"]), atol=1e-7)
    filled["cash_fee"] = filled[["commission", "transfer_fee", "stamp_tax"]].sum(axis=1)
    filled["total_friction"] = filled.cash_fee + filled.slippage_cost
    filled["effective_friction_bps"] = filled.total_friction / filled.notional * 10000
    filled["notional_bucket"] = pd.cut(
        filled.notional, bins=config["notional_buckets"],
        labels=config["bucket_labels"], right=False)

    flow = filled.groupby("side", observed=True).agg(
        fills=("fill_id", "size"), notional=("notional", "sum"),
        commission=("commission", "sum"), transfer_fee=("transfer_fee", "sum"),
        stamp_tax=("stamp_tax", "sum"), slippage=("slippage_cost", "sum"),
        minimum_commission_hits=("minimum_commission_hit", "sum"),
        minimum_commission_excess=("minimum_commission_excess", "sum"),
    ).reset_index()
    by_bucket = filled.groupby(["side", "notional_bucket"], observed=True).agg(
        fills=("fill_id", "size"), notional=("notional", "sum"),
        total_friction=("total_friction", "sum"),
        median_effective_friction_bps=("effective_friction_bps", "median"),
        minimum_commission_hits=("minimum_commission_hit", "sum"),
        minimum_commission_excess=("minimum_commission_excess", "sum"),
    ).reset_index()

    buys = filled[filled.side.eq("buy")].merge(
        orders[["intent_id", "portfolio_equity_asof"]], on="intent_id",
        how="left", validate="one_to_one")
    buys["entry_weight"] = (
        buys.notional + buys.commission + buys.transfer_fee + buys.stamp_tax
    ) / buys.portfolio_equity_asof
    buys["small_entry"] = buys.entry_weight < float(
        config["small_entry_equity_fraction"])

    lot_entry = lots.groupby("trade_id", observed=True).agg(
        entry_notional=("original_quantity", lambda s: 0.0),
        entry_commission=("allocated_commission_cash", "sum"),
        entry_transfer=("allocated_transfer_fee_cash", "sum"),
        entry_slippage=("allocated_slippage_cash", "sum"),
    ).reset_index()
    # Quantity and price must be multiplied row by row before aggregation.
    lots = lots.copy()
    lots["entry_notional_row"] = lots.original_quantity * lots.fill_price_raw
    entry_notional = lots.groupby("trade_id").entry_notional_row.sum()
    lot_entry["entry_notional"] = lot_entry.trade_id.map(entry_notional)
    trade_exit = disp.groupby("trade_id", observed=True).agg(
        symbol=("symbol", "first"), realized_pnl=("realized_pnl_cash", "sum"),
        exit_notional=("allocated_gross_proceeds_cash", "sum"),
        exit_commission=("allocated_sell_commission_cash", "sum"),
        exit_transfer=("allocated_sell_transfer_fee_cash", "sum"),
        exit_stamp=("allocated_stamp_tax_cash", "sum"),
        exit_slippage=("allocated_sell_slippage_cash", "sum"),
        exit_date=("fill_date", "max"), exit_reason=("exit_reason", "last"),
    ).reset_index()
    trades = lot_entry.merge(trade_exit, on="trade_id", how="inner", validate="one_to_one")
    trades["net_return_pct"] = trades.realized_pnl / (
        trades.entry_notional + trades.entry_commission + trades.entry_transfer) * 100
    trades["round_trip_friction"] = trades[[
        "entry_commission", "entry_transfer", "entry_slippage",
        "exit_commission", "exit_transfer", "exit_stamp", "exit_slippage"]].sum(axis=1)
    trades["round_trip_friction_bps"] = trades.round_trip_friction / trades.entry_notional * 10000
    trades["notional_bucket"] = pd.cut(
        trades.entry_notional, bins=config["notional_buckets"],
        labels=config["bucket_labels"], right=False)
    outcome = trades.groupby("notional_bucket", observed=True).agg(
        closed_trades=("trade_id", "size"), entry_notional=("entry_notional", "sum"),
        realized_pnl=("realized_pnl", "sum"), mean_net_return_pct=("net_return_pct", "mean"),
        median_net_return_pct=("net_return_pct", "median"),
        win_rate_pct=("realized_pnl", lambda s: float((s > 0).mean() * 100)),
        total_friction=("round_trip_friction", "sum"),
        median_round_trip_friction_bps=("round_trip_friction_bps", "median"),
    ).reset_index()

    components = {
        "commission": float(filled.commission.sum()),
        "transfer_fee": float(filled.transfer_fee.sum()),
        "stamp_tax": float(filled.stamp_tax.sum()),
        "slippage": float(filled.slippage_cost.sum()),
    }
    total = sum(components.values())
    summary = {
        "source_backtest": str(source),
        "source_period": [int(filled.date.min()), int(filled.date.max())],
        "filled_buys": int((filled.side == "buy").sum()),
        "filled_sells": int((filled.side == "sell").sum()),
        "total_friction_cash": total,
        "total_friction_pct_initial": total / float(config["initial_cash"]) * 100,
        "friction_components_cash": components,
        "friction_component_share_pct": {
            key: value / total * 100 for key, value in components.items()},
        "minimum_commission_breakpoint_cash": (
            float(config["minimum_commission"]) / float(config["broker_rate"])),
        "minimum_commission_hit_fills": int(filled.minimum_commission_hit.sum()),
        "minimum_commission_hit_share_pct": float(filled.minimum_commission_hit.mean() * 100),
        "minimum_commission_excess_cash": float(filled.minimum_commission_excess.sum()),
        "minimum_commission_excess_pct_initial": float(
            filled.minimum_commission_excess.sum() / float(config["initial_cash"]) * 100),
        "small_buy_count": int(buys.small_entry.sum()),
        "small_buy_share_pct": float(buys.small_entry.mean() * 100),
        "small_buy_notional_share_pct": float(
            buys.loc[buys.small_entry, "notional"].sum() / buys.notional.sum() * 100),
        "reported_fixed_path_reference_return_pct": metrics.get("fixed_path_reference_return_pct"),
        "reported_net_return_pct": metrics.get("return_pct"),
        "reported_total_friction_pct_initial": metrics.get("total_friction_pct_initial"),
        "decision": "DIAGNOSTIC_ONLY_NO_ORDER_FILTER",
        "reason": (
            "Minimum-commission excess is measured, but removing small orders would "
            "change holdings, exits, risk headroom and later orders. Bucket outcomes are "
            "descriptive and are not a causal backtest or a basis for choosing a cutoff."),
    }
    flow.to_csv(output / "friction_by_side.csv", index=False)
    by_bucket.to_csv(output / "friction_by_notional_bucket.csv", index=False)
    trades.to_csv(output / "closed_trade_attribution.csv", index=False)
    outcome.to_csv(output / "closed_trade_outcomes_by_entry_notional.csv", index=False)
    buys[["date", "symbol", "notional", "portfolio_equity_asof", "entry_weight",
          "small_entry", "minimum_commission_hit", "minimum_commission_excess",
          "effective_friction_bps"]].to_csv(output / "buy_entry_diagnostics.csv", index=False)
    _write_json(output / "summary.json", summary)

    lines = [
        "# Alpha158 执行成本与小额订单归因", "",
        "本报告只做冻结成交路径的只读归因。2 万元分界由 5 元最低佣金除以万分之 2.5 佣金率得到，未根据收益寻找阈值；没有删除订单、重放账户或改变正式策略。", "",
        "## 总成本", "",
        f"成交期为 {summary['source_period'][0]}—{summary['source_period'][1]}，共 {summary['filled_buys']} 笔买入、{summary['filled_sells']} 笔卖出。总摩擦成本 {total:,.2f} 元，占初始资金 {_pct(summary['total_friction_pct_initial'])}。", "",
        "| 成本项 | 金额 | 成本占比 |", "|---|---:|---:|",
    ]
    labels = {"commission": "佣金", "transfer_fee": "过户费",
              "stamp_tax": "印花税", "slippage": "滑点"}
    for key, value in components.items():
        lines.append(f"| {labels[key]} | {value:,.2f} | {summary['friction_component_share_pct'][key]:.2f}% |")
    lines += ["", "## 最低佣金与名义金额", "",
              f"{summary['minimum_commission_hit_fills']} 笔成交触发最低佣金，占全部成交 {summary['minimum_commission_hit_share_pct']:.1f}%。相对纯比例佣金，多付 {summary['minimum_commission_excess_cash']:,.2f} 元，仅占初始资金 {_pct(summary['minimum_commission_excess_pct_initial'])}。", "",
              "| 方向 | 名义金额区间 | 笔数 | 摩擦成本 | 摩擦成本中位数（bp） | 最低佣金笔数 |", "|---|---|---:|---:|---:|---:|"]
    for row in by_bucket.itertuples():
        lines.append(f"| {row.side} | {row.notional_bucket} | {row.fills} | {row.total_friction:,.2f} | {row.median_effective_friction_bps:.2f} | {row.minimum_commission_hits} |")
    lines += ["", "## 已平仓交易的描述性结果", "",
              "下表按入场金额固定分组，净收益来自真实账本的已实现盈亏。它可以显示小单是否只是成本问题，但不能模拟删除小单后的账户路径。", "",
              "| 入场金额 | 已平仓数 | 已实现盈亏 | 平均净收益 | 胜率 | 往返摩擦中位数（bp） |", "|---|---:|---:|---:|---:|---:|"]
    for row in outcome.itertuples():
        lines.append(f"| {row.notional_bucket} | {row.closed_trades} | {row.realized_pnl:,.2f} | {row.mean_net_return_pct:.2f}% | {row.win_rate_pct:.1f}% | {row.median_round_trip_friction_bps:.2f} |")
    lines += ["", "## 决策", "",
              "不增加最小下单金额过滤。最低佣金确实使小额成交的有效费率更高，但可节省的最低佣金增量很小；滑点才是主要摩擦。直接取消小单会同时改变持仓、风险额度和后续机会，必须以预登记的完整路径实验验证，不能依据上述分组收益挑阈值。此前等风险分配虽减少小单，却没有通过跨年度收益稳定性门槛，因此继续保留当前执行方式。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    print(json.dumps(analyze(config, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

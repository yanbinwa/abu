#!/usr/bin/env python3
"""Diagnose volume/sentiment-aware exits for selected Alpha158 winners.

This is a post-hoc diagnostic only.  It does not mutate the frozen strategy or
claim an implementable portfolio return because replacement entries and cash
reuse are deliberately not replayed here.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


BACKTEST = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_factor_utility_interim_replay_20261007/all_mean_rank_25bp"
)
VISUAL = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_mean_rank_25bp_trade_visualization_20261007"
)
RAW = Path("/Users/wjy/abu/data/selection_research_2015_v1/raw")
ADJUSTED = Path("/Users/wjy/abu/data/csv")
OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_exit_sentiment_case_analysis_20261007"
)
CASE_SYMBOLS = ("sz300755", "sz300435", "sz300138", "sz001336", "sz002375")
KEY_DATES = (20240930, 20241008, 20241009, 20241010, 20250321, 20250324, 20250325)


def adjusted_path(symbol: str) -> Path:
    matches = list(ADJUSTED.glob(f"{symbol}_*"))
    if not matches:
        raise FileNotFoundError(symbol)
    return matches[0]


def load_price(symbol: str) -> pd.DataFrame:
    raw = pd.read_csv(RAW / f"{symbol}.csv")
    adj = pd.read_csv(adjusted_path(symbol), usecols=["date", "p_change", "pre_close"])
    raw["date"] = raw["date"].astype(int)
    adj["date"] = adj["date"].astype(int)
    frame = raw.merge(adj, on="date", how="left").sort_values("date").reset_index(drop=True)
    frame["volume_median_20"] = frame["volume"].rolling(20, min_periods=20).median()
    frame["volume_ratio_20"] = frame["volume"] / frame["volume_median_20"]
    frame["turnover_pct"] = frame["turnover"] * 100.0
    frame["range_pct"] = (frame["high"] - frame["low"]) / frame["pre_close"] * 100.0
    frame["giveback_pct"] = (1.0 - frame["close"] / frame["high"]) * 100.0
    return frame


def select_cases(operations: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for trade_id, group in operations.groupby("trade_id"):
        group = group.sort_values("date")
        entry, exit_ = group.iloc[0], group.iloc[-1]
        if entry["symbol"] not in CASE_SYMBOLS or exit_["marker_type"] != "CLOSE":
            continue
        # The five user-selected charts refer to the common 2024 market event.
        if int(entry["date"]) >= 20250000:
            continue
        prices = load_price(entry["symbol"])
        start = prices.index[prices["date"].eq(int(entry["date"]))][0]
        end = prices.index[prices["date"].eq(int(exit_["date"]))][-1]
        held = prices.loc[start:end]
        peak_index = int(held["high"].idxmax())
        peak = prices.loc[peak_index]
        trigger = prices.loc[prices["date"].eq(20241008)].iloc[0]
        next_open = prices.loc[prices["date"].eq(20241009)].iloc[0]
        alternate_fill = float(next_open["open"]) * (1.0 - 25.0 / 10000.0)
        actual_fill = float(exit_["price_raw"])
        quantity = float(entry["quantity"])
        prior_session = prices.loc[end - 1, "date"] if end > 0 else np.nan
        rows.append(
            {
                "trade_id": trade_id,
                "symbol": entry["symbol"],
                "entry_date": int(entry["date"]),
                "entry_fill": float(entry["price_raw"]),
                "peak_date": int(peak["date"]),
                "peak_high": float(peak["high"]),
                "peak_close": float(peak["close"]),
                "volume_ratio_20_at_peak": float(trigger["volume_ratio_20"]),
                "turnover_pct_at_peak": float(trigger["turnover_pct"]),
                "intraday_giveback_pct": float(trigger["giveback_pct"]),
                "actual_stop_signal_date": int(prior_session),
                "actual_exit_date": int(exit_["date"]),
                "actual_exit_fill": actual_fill,
                "actual_realized_pnl": float(group["realized_pnl_cash"].sum()),
                "next_open_sentiment_exit_fill": alternate_fill,
                "fill_improvement_pct": (alternate_fill / actual_fill - 1.0) * 100.0,
                "diagnostic_pnl_improvement": (alternate_fill - actual_fill) * quantity,
                "peak_to_actual_exit_sessions": int(end - peak_index),
                "overnight_gap_after_signal_pct":
                    (float(next_open["open"]) / float(trigger["close"]) - 1.0) * 100.0,
            }
        )
    return pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)


def market_breadth() -> pd.DataFrame:
    rows = []
    target_dates = set(KEY_DATES)
    for raw_path in RAW.glob("*.csv"):
        symbol = raw_path.stem
        matches = list(ADJUSTED.glob(f"{symbol}_*"))
        if not matches:
            continue
        frame = pd.read_csv(matches[0], usecols=["date", "p_change", "volume"])
        frame["date"] = pd.to_numeric(frame["date"], errors="coerce").astype("Int64")
        frame["volume_ratio_20"] = (
            frame["volume"] / frame["volume"].rolling(20, min_periods=20).median()
        )
        for row in frame[frame["date"].isin(target_dates)].itertuples(index=False):
            rows.append((int(row.date), symbol, float(row.p_change), float(row.volume_ratio_20)))
    panel = pd.DataFrame(rows, columns=["date", "symbol", "return_pct", "volume_ratio_20"])
    out = []
    for date, group in panel.groupby("date"):
        out.append(
            {
                "date": int(date),
                "stock_count": len(group),
                "advance_pct": float(group["return_pct"].gt(0).mean() * 100.0),
                "decline_pct": float(group["return_pct"].lt(0).mean() * 100.0),
                "median_return_pct": float(group["return_pct"].median()),
                "limit_up_like_count": int(group["return_pct"].ge(9.5).sum()),
                "limit_down_like_count": int(group["return_pct"].le(-9.5).sum()),
                "median_volume_ratio_20": float(group["volume_ratio_20"].median()),
                "volume_ratio_ge_3_pct": float(group["volume_ratio_20"].ge(3).mean() * 100.0),
            }
        )
    return pd.DataFrame(out).sort_values("date").reset_index(drop=True)


def holdings_counterfactual(operations: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for trade_id, group in operations.groupby("trade_id"):
        group = group.sort_values("date")
        entry, exit_ = group.iloc[0], group.iloc[-1]
        if not (int(entry["date"]) <= 20241008 < int(exit_["date"])):
            continue
        prices = load_price(entry["symbol"])
        next_open = prices.loc[prices["date"].eq(20241009)]
        if next_open.empty:
            continue
        alternate_fill = float(next_open.iloc[0]["open"]) * (1.0 - 25.0 / 10000.0)
        actual_fill = float(exit_["price_raw"])
        quantity = float(entry["quantity"])
        rows.append(
            {
                "trade_id": trade_id,
                "symbol": entry["symbol"],
                "entry_date": int(entry["date"]),
                "actual_exit_date": int(exit_["date"]),
                "actual_exit_fill": actual_fill,
                "sentiment_exit_fill": alternate_fill,
                "diagnostic_pnl_improvement": (alternate_fill - actual_fill) * quantity,
                "actual_realized_pnl": float(group["realized_pnl_cash"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("diagnostic_pnl_improvement", ascending=False)


def individual_rule_robustness(operations: pd.DataFrame) -> pd.DataFrame:
    # Fixed, interpretable diagnostic.  Thresholds are intentionally not optimized.
    rows = []
    for trade_id, group in operations.groupby("trade_id"):
        group = group.sort_values("date")
        entry, exit_ = group.iloc[0], group.iloc[-1]
        if exit_["marker_type"] != "CLOSE":
            continue
        prices = load_price(entry["symbol"])
        start_index = prices.index[prices["date"].eq(int(entry["date"]))]
        end_index = prices.index[prices["date"].eq(int(exit_["date"]))]
        if start_index.empty or end_index.empty:
            continue
        start, end = int(start_index[0]), int(end_index[-1])
        adjusted_entry = float(entry["price_raw"]) * float(prices.loc[start, "close"]) / float(prices.loc[start, "open"])
        prices["gain_from_entry_pct"] = (prices["close"] / adjusted_entry - 1.0) * 100.0
        eligible = prices.loc[start : max(start, end - 1)]
        hit = eligible[
            eligible["gain_from_entry_pct"].ge(15.0)
            & eligible["volume_ratio_20"].ge(3.0)
            & eligible["range_pct"].ge(8.0)
            & eligible["giveback_pct"].ge(4.0)
        ]
        if hit.empty:
            continue
        trigger_index = int(hit.index[0])
        if trigger_index + 1 >= len(prices):
            continue
        next_bar = prices.loc[trigger_index + 1]
        alternate_fill = float(next_bar["open"]) * (1.0 - 25.0 / 10000.0)
        actual_fill = float(exit_["price_raw"])
        quantity = float(entry["quantity"])
        rows.append(
            {
                "trade_id": trade_id,
                "symbol": entry["symbol"],
                "trigger_date": int(hit.iloc[0]["date"]),
                "trigger_year": int(hit.iloc[0]["date"]) // 10000,
                "diagnostic_pnl_improvement": (alternate_fill - actual_fill) * quantity,
            }
        )
    detail = pd.DataFrame(rows)
    if detail.empty:
        return detail
    return (
        detail.groupby("trigger_year")
        .agg(
            trades=("trade_id", "size"),
            pnl_improvement=("diagnostic_pnl_improvement", "sum"),
            improved=("diagnostic_pnl_improvement", lambda x: int(x.gt(1).sum())),
            worsened=("diagnostic_pnl_improvement", lambda x: int(x.lt(-1).sum())),
        )
        .reset_index()
    )


def index_snapshot() -> dict:
    frame = pd.read_csv(adjusted_path("sh000300"))
    frame["date"] = frame["date"].astype(int)
    frame["volume_ratio_20"] = frame["volume"] / frame["volume"].rolling(20, min_periods=20).median()
    frame["giveback_pct"] = (1.0 - frame["close"] / frame["high"]) * 100.0
    frame["return_5d_pct"] = (frame["close"] / frame["close"].shift(5) - 1.0) * 100.0
    row = frame.loc[frame["date"].eq(20241008)].iloc[0]
    return {key: float(row[key]) for key in ("p_change", "volume_ratio_20", "giveback_pct", "return_5d_pct")}


def plot_cases(cases: pd.DataFrame, breadth: pd.DataFrame) -> None:
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2))
    ordered = cases.sort_values("diagnostic_pnl_improvement")
    axes[0].barh(ordered["symbol"], ordered["diagnostic_pnl_improvement"], color="#2563eb")
    axes[0].set_title("10月8日收盘识别、次日开盘退出的诊断增益")
    axes[0].set_xlabel("相对实际退出的盈亏改善（元）")
    key = breadth[breadth["date"].isin([20240930, 20241008, 20241009])]
    labels = key["date"].astype(str)
    axes[1].plot(labels, key["advance_pct"], marker="o", label="上涨家数占比")
    axes[1].plot(labels, key["decline_pct"], marker="o", label="下跌家数占比")
    axes[1].set_ylim(0, 105)
    axes[1].set_ylabel("占比（%）")
    axes[1].set_title("全市场宽度在 2024-10-08 后反转")
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(OUTPUT / "sentiment_exit_diagnostic.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    operations = pd.read_csv(VISUAL / "operations_visualized.csv")
    cases = select_cases(operations)
    breadth = market_breadth()
    holdings = holdings_counterfactual(operations)
    robustness = individual_rule_robustness(operations)
    index = index_snapshot()
    nav = pd.read_csv(BACKTEST / "daily_nav.csv")
    nav_1008 = float(nav.loc[nav["date"].eq(20241008), "capital"].iloc[0])

    cases.to_csv(OUTPUT / "selected_case_diagnostics.csv", index=False)
    breadth.to_csv(OUTPUT / "market_breadth_key_dates.csv", index=False)
    holdings.to_csv(OUTPUT / "market_climax_counterfactual_20241008.csv", index=False)
    robustness.to_csv(OUTPUT / "individual_rule_robustness.csv", index=False)
    plot_cases(cases, breadth)

    key = breadth.set_index("date")
    case_rows = "\n".join(
        f"| {r.symbol} | {r.volume_ratio_20_at_peak:.2f}x | {r.intraday_giveback_pct:.2f}% | "
        f"{int(r.peak_to_actual_exit_sessions)} | {r.fill_improvement_pct:.2f}% | "
        f"{r.diagnostic_pnl_improvement:,.0f} |"
        for r in cases.itertuples(index=False)
    )
    robust_rows = "\n".join(
        f"| {int(r.trigger_year)} | {int(r.trades)} | {int(r.improved)} | "
        f"{int(r.worsened)} | {r.pnl_improvement:,.0f} |"
        for r in robustness.itertuples(index=False)
    )
    total_delta = float(holdings["diagnostic_pnl_improvement"].sum())
    report = f"""# Alpha158 高位情绪退出案例诊断

## 结论

这五笔 2024 年交易不是五个独立事件，而是同一个 **2024-10-08 市场情绪顶部**。
当前 `event_exit_only` 的移动止损仅使用峰值收盘价和 3ATR；成交量、换手率、涨跌停扩散与指数日内回落均不参与退出判定。因此它能保留连板趋势，但在市场整体退潮时反应偏慢。

10 月 8 日沪深300单日上涨 {index['p_change']:.2f}%，但成交量达到20日中位数的 {index['volume_ratio_20']:.2f} 倍、收盘较日内高点回落 {index['giveback_pct']:.2f}%，5日累计上涨 {index['return_5d_pct']:.2f}%。全市场样本中，{key.loc[20241008, 'volume_ratio_ge_3_pct']:.1f}% 的股票成交量达到20日中位数3倍以上。次日上涨家数占比从 {key.loc[20241008, 'advance_pct']:.1f}% 降到 {key.loc[20241009, 'advance_pct']:.1f}%，中位数收益从 {key.loc[20241008, 'median_return_pct']:.2f}% 变为 {key.loc[20241009, 'median_return_pct']:.2f}%。

## 用户点名的五笔交易

| 股票 | 峰值日量比 | 日内高点回撤 | 峰值到实际退出交易日 | 次日开盘退出价格改善 | 诊断盈亏改善（元） |
|---|---:|---:|---:|---:|---:|
{case_rows}

如果在 10 月 8 日收盘确认市场级情绪耗竭，并按策略现有 D+1 模型于 10 月 9 日开盘退出，这五笔相对实际退出合计约改善 **{cases['diagnostic_pnl_improvement'].sum():,.0f} 元**。当日组合共持有 {len(holdings)} 只股票，全部十只的同口径诊断改善约 **{total_delta:,.0f} 元**，相当于 10 月 8 日净值的 **{total_delta/nav_1008*100:.2f}%**。

该数字没有计入退出后现金再投资，因此不是正式回测收益增量。

`sz001336` 还有一笔 2025-01-07 至 2025-03-25 的交易，形态不同：3 月 18 日见顶后，3 月 21 日全市场上涨家数占比已经降至 {key.loc[20250321, 'advance_pct']:.1f}%；3 月 24 日个股下跌 8.17%、成交量达到20日中位数的3.13倍。现有移动止损在 3 月 24 日收盘触发并于次日开盘退出，已经是 D+1 模型下对个股破位的最快反应。它提示市场宽度可能用于更早减仓，但不能说明 ATR 执行本身在破位后又多等待了一天。

## 为什么不能直接增加“个股巨量退出”

固定的个股耗竭诊断条件为：持仓收益至少15%、成交量至少20日中位数3倍、日内振幅至少8%、收盘较最高价回落至少4%。它没有调参，但年度结果明显不稳定：

| 触发年度 | 交易数 | 改善 | 恶化 | 诊断盈亏增量（元） |
|---:|---:|---:|---:|---:|
{robust_rows}

2025 年的负贡献主要来自过早卖出后续长期上涨的 `sz301150` 和 `sz301232`。因此，单只股票放量冲高回落既可能是顶部，也可能只是主升浪换手；把它直接作为强制清仓规则会损失右尾收益。

## 建议的下一项冻结实验

只研究一个稀疏的**市场级风险释放覆盖层**，不改变选股因子和入场：

1. 使用前一交易日收盘后可得的数据；
2. 同时要求指数短期涨幅、指数成交量异常、指数冲高回落及市场宽度极端；
3. 信号触发后，下一交易日开盘减仓或退出已有盈利持仓；
4. 与“仅个股量价退出”和原 3ATR 退出做消融；
5. 在 2015–2026 按年度、25/40/60bp、涨跌停不可成交约束下检验；
6. 该规则只有在多个独立市场周期中改善 ES95/最大回撤，且不明显损伤长期赢家时才进入 shadow。

目前证据足以说明退出层缺少市场情绪输入，但不足以证明一个具体阈值已经可以上线。2024-10-08 只能计作一个独立事件。
"""
    (OUTPUT / "REPORT.md").write_text(report, encoding="utf-8")
    summary = {
        "selected_cases": len(cases),
        "same_event_date": 20241008,
        "selected_case_diagnostic_delta": float(cases["diagnostic_pnl_improvement"].sum()),
        "all_holdings_on_event": len(holdings),
        "all_holdings_diagnostic_delta": total_delta,
        "delta_as_pct_of_nav": total_delta / nav_1008 * 100.0,
        "individual_rule_years": robustness.to_dict(orient="records"),
        "research_only": True,
        "strategy_mutated": False,
    }
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

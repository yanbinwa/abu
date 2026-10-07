#!/usr/bin/env python3
"""Attribute every Alpha158 trade to market, industry and stock context."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuMarketIndustryExit import (  # noqa: E402
    MarketIndustryAbsoluteState, load_market_industry_exit_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402


BACKTEST = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_factor_utility_interim_replay_20261007/"
    "all_mean_rank_25bp")
OPERATIONS = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_mean_rank_25bp_trade_visualization_20261007/"
    "operations_visualized.csv")
OUTPUT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_trade_market_industry_stock_v1_20261007")
CONFIG = ROOT / "configs/selection/alpha158_market_industry_exit_v1.json"


def safe_ratio(numerator, denominator):
    return (float(numerator) / float(denominator)
            if np.isfinite(numerator) and np.isfinite(denominator) and
            denominator != 0 else np.nan)


class ContextFeatures:
    def __init__(self, panel, absolute):
        self.panel = panel
        self.absolute = absolute
        self.denominator = panel.breadth_denominator(
            min_history=120, unknown_st_policy="include")
        self.industry_cache = {}

    def _returns(self, day, horizon):
        if day < horizon:
            return np.full(len(self.panel.symbols), np.nan)
        current = np.asarray(self.panel.close[day], dtype=float)
        previous = np.asarray(self.panel.close[day - horizon], dtype=float)
        result = np.full(current.shape, np.nan)
        np.divide(current, previous, out=result,
                  where=np.isfinite(current) & np.isfinite(previous) &
                  (previous > 0))
        return result - 1.0

    def _industry_map(self, day, horizon):
        key = (int(day), int(horizon))
        if key in self.industry_cache:
            return self.industry_cache[key]
        values = self._returns(day, horizon)
        membership = np.asarray(self.panel.industry[day], dtype=int)
        eligible = self.denominator[day] & np.isfinite(values)
        result = {}
        for industry in np.unique(membership[eligible & (membership >= 0)]):
            members = eligible & (membership == industry)
            if members.sum() >= 10:
                result[int(industry)] = float(np.mean(values[members]))
        ordered = sorted(result, key=lambda item: (result[item], item))
        rank = {industry: (index + 1) / len(ordered)
                for index, industry in enumerate(ordered)} if ordered else {}
        self.industry_cache[key] = (result, rank)
        return result, rank

    def at(self, day, column):
        panel = self.panel
        industry = int(panel.industry[day, column])
        market = self.absolute.market_features.iloc[day]
        output = {
            "date": int(panel.dates[day]),
            "industry_id": industry,
            "market_reason": str(market.reason or ""),
            "market_return_5d": float(market.return_5d),
            "market_intraday_return": float(market.intraday_return),
            "market_amount_ratio_20": float(market.amount_ratio_20),
            "market_advance_ratio": float(market.advance_ratio),
        }
        industry_reason, _ = self.absolute.industry_signal(day, panel.symbols[column])
        output["industry_reason"] = str(industry_reason or "")
        for horizon in (20, 60):
            stock = self._returns(day, horizon)[column]
            by_industry, ranks = self._industry_map(day, horizon)
            industry_return = by_industry.get(industry, np.nan)
            if day >= horizon:
                benchmark = (panel.benchmark_close[day] /
                             panel.benchmark_close[day - horizon] - 1.0)
            else:
                benchmark = np.nan
            output[f"market_return_{horizon}d"] = float(benchmark)
            output[f"industry_return_{horizon}d"] = float(industry_return)
            output[f"industry_relative_{horizon}d"] = float(
                industry_return - benchmark)
            output[f"industry_rank_{horizon}d"] = float(
                ranks.get(industry, np.nan))
            output[f"stock_return_{horizon}d"] = float(stock)
            output[f"stock_industry_relative_{horizon}d"] = float(
                stock - industry_return)
        amount = np.asarray(panel.amount[max(0, day - 20):day], dtype=float)
        median = np.nanmedian(amount[:, column]) if len(amount) else np.nan
        output["stock_amount_ratio_20"] = safe_ratio(
            panel.amount[day, column], median)
        output["stock_intraday_return"] = safe_ratio(
            panel.close[day, column], panel.open[day, column]) - 1.0
        daily = np.asarray(panel.returns[max(0, day - 19):day + 1, column],
                           dtype=float)
        output["stock_volatility_20"] = float(np.nanstd(daily, ddof=1))
        history = np.asarray(panel.close[max(0, day - 19):day + 1, column],
                             dtype=float)
        output["stock_drawdown_from_20d_high"] = float(
            safe_ratio(panel.close[day, column], np.nanmax(history)) - 1.0)
        return output


def previous_day(day):
    return max(0, int(day) - 1)


def prefix(values, name):
    return {f"{name}_{key}": value for key, value in values.items()
            if key != "date"}


def alignment(row):
    states = (
        "M+" if row["entry_market_return_20d"] > 0 else "M-",
        "I+" if row["entry_industry_relative_20d"] > 0 else "I-",
        "S+" if row["entry_stock_industry_relative_20d"] > 0 else "S-",
    )
    return "/".join(states)


def trade_rows(panel, features, operations, lineage):
    date_index = {int(date): index for index, date in enumerate(panel.dates)}
    lineage = lineage.set_index("trade_id")
    rows = []
    for trade_id, group in operations.groupby("trade_id", sort=False):
        group = group.sort_values("date")
        entry = group[group.marker_type.eq("OPEN")]
        if entry.empty:
            continue
        entry = entry.iloc[0]
        closes = group[group.marker_type.eq("CLOSE")]
        closed = not closes.empty
        exit_fill = closes.iloc[-1] if closed else None
        symbol = str(entry.symbol)
        if symbol not in panel.symbol_index or int(entry.date) not in date_index:
            continue
        column = panel.symbol_index[symbol]
        entry_day = date_index[int(entry.date)]
        end_day = (date_index[int(exit_fill.date)] if closed and
                   int(exit_fill.date) in date_index else len(panel.dates) - 1)
        if end_day < entry_day:
            continue
        held_close = np.asarray(panel.close[entry_day:end_day + 1, column], float)
        held_high = np.asarray(panel.high[entry_day:end_day + 1, column], float)
        held_low = np.asarray(panel.low[entry_day:end_day + 1, column], float)
        peak_day = entry_day + int(np.nanargmax(held_high))
        trough_day = entry_day + int(np.nanargmin(held_low))
        entry_context_day = previous_day(entry_day)
        exit_context_day = previous_day(end_day)
        record = lineage.loc[trade_id]
        entry_adjusted = float(record.initial_entry_price_adjusted)
        initial_r = float(record.initial_r_per_share_adjusted)
        risk_cash = float(record.initial_r_cash_frozen)
        realized_cash = float(group.realized_pnl_cash.sum())
        quantity = int(entry.quantity)
        if closed:
            factor = safe_ratio(panel.close[end_day, column],
                                panel.exec_close[end_day, column])
            exit_adjusted = float(exit_fill.price_raw) * factor
            fill_return = float(exit_fill.price_raw / entry.price_raw - 1.0)
        else:
            exit_adjusted = float(panel.close[end_day, column])
            fill_return = np.nan
        peak_price = float(np.nanmax(held_high))
        trough_price = float(np.nanmin(held_low))
        row = {
            "trade_id": trade_id, "symbol": symbol,
            "entry_date": int(entry.date),
            "exit_date": int(exit_fill.date) if closed else None,
            "status": "CLOSED" if closed else "OPEN",
            "exit_reason": str(exit_fill.reason_code) if closed else "",
            "quantity": quantity,
            "holding_sessions": int(end_day - entry_day + 1),
            "entry_price_raw": float(entry.price_raw),
            "exit_price_raw": float(exit_fill.price_raw) if closed else None,
            "fill_return": fill_return,
            "initial_r_cash": risk_cash,
            "realized_pnl_cash": realized_cash,
            "realized_r": realized_cash / risk_cash if risk_cash > 0 else np.nan,
            "peak_date": int(panel.dates[peak_day]),
            "trough_date": int(panel.dates[trough_day]),
            "mfe_r": (peak_price - entry_adjusted) / initial_r,
            "mae_r": (trough_price - entry_adjusted) / initial_r,
            "peak_to_exit_giveback_r": (
                (peak_price - exit_adjusted) / initial_r if closed else np.nan),
        }
        row.update(prefix(features.at(entry_context_day, column), "entry"))
        row.update(prefix(features.at(peak_day, column), "peak"))
        row.update(prefix(features.at(exit_context_day, column), "exit"))

        trailing = False
        peak_close = entry_adjusted
        market_date = market_reason = None
        industry_date = industry_reason = None
        # Exclude the actual exit-signal close; base exits retain priority.
        scan_end = max(entry_day, exit_context_day - int(closed))
        for day in range(entry_day, scan_end + 1):
            close = float(panel.close[day, column])
            if not np.isfinite(close):
                continue
            peak_close = max(peak_close, close)
            if initial_r > 0 and peak_close - entry_adjusted >= initial_r:
                trailing = True
            if not trailing:
                continue
            if market_date is None:
                reason = features.absolute.market_signal(day)
                if reason:
                    market_date, market_reason = int(panel.dates[day]), reason
            if industry_date is None:
                reason, _ = features.absolute.industry_signal(day, symbol)
                if reason:
                    industry_date, industry_reason = int(panel.dates[day]), reason
        candidates = [(market_date, "MARKET", market_reason),
                      (industry_date, "INDUSTRY", industry_reason)]
        candidates = [item for item in candidates if item[0] is not None]
        if candidates:
            signal_date, layer, reason = min(candidates)
            signal_day = date_index[signal_date]
            execution_day = signal_day + 1
            row.update(prefix(features.at(signal_day, column),
                              "context_signal"))
            if execution_day <= end_day:
                alternate_fill = float(
                    panel.exec_open[execution_day, column] * (1 - 25 / 10000))
                improvement = ((alternate_fill - float(exit_fill.price_raw)) *
                               quantity if closed else np.nan)
                improvement_r = improvement / risk_cash if risk_cash > 0 else np.nan
            else:
                alternate_fill = improvement = improvement_r = np.nan
            row.update({
                "first_context_signal_date": signal_date,
                "first_context_signal_layer": layer,
                "first_context_signal_reason": reason,
                "context_exit_fill_raw": alternate_fill,
                "context_exit_improvement_cash": improvement,
                "context_exit_improvement_r": improvement_r,
            })
        else:
            row.update({
                "context_signal_market_reason": "",
                "context_signal_industry_reason": "",
            })
            row.update({
                "first_context_signal_date": None,
                "first_context_signal_layer": "",
                "first_context_signal_reason": "",
                "context_exit_fill_raw": None,
                "context_exit_improvement_cash": None,
                "context_exit_improvement_r": None,
            })
        row["entry_alignment"] = alignment(row)
        rows.append(row)
    return pd.DataFrame(rows)


def entry_model(trades):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, brier_score_loss
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    features = [
        "entry_market_return_20d", "entry_market_advance_ratio",
        "entry_industry_return_20d", "entry_industry_relative_20d",
        "entry_industry_rank_20d", "entry_stock_return_20d",
        "entry_stock_industry_relative_20d", "entry_stock_amount_ratio_20",
        "entry_stock_volatility_20", "entry_stock_drawdown_from_20d_high",
    ]
    data = trades[trades.status.eq("CLOSED")].copy()
    data["year"] = data.entry_date.astype(int) // 10000
    data["target"] = data.realized_r > 0
    predictions = []
    rows = []
    for year in sorted(data.year.unique()):
        train, test = data[data.year.ne(year)], data[data.year.eq(year)]
        if train.target.nunique() < 2 or test.target.nunique() < 2:
            continue
        model = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(
                C=0.1, class_weight="balanced", max_iter=2000,
                random_state=20261007)),
        ])
        model.fit(train[features], train.target.astype(int))
        probability = model.predict_proba(test[features])[:, 1]
        rows.append({
            "test_year": int(year), "trades": int(len(test)),
            "win_rate": float(test.target.mean()),
            "auc": float(roc_auc_score(test.target, probability)),
            "brier": float(brier_score_loss(test.target, probability)),
        })
        part = test[["trade_id", "symbol", "entry_date", "realized_r"]].copy()
        part["test_year"] = int(year)
        part["predicted_win_probability"] = probability
        predictions.append(part)
    return pd.DataFrame(rows), (pd.concat(predictions, ignore_index=True)
                                if predictions else pd.DataFrame())


def summarize(trades):
    closed = trades[trades.status.eq("CLOSED")].copy()
    alignment_summary = closed.groupby("entry_alignment").agg(
        trades=("trade_id", "size"),
        win_rate=("realized_r", lambda x: float((x > 0).mean())),
        mean_r=("realized_r", "mean"), median_r=("realized_r", "median"),
        mean_mfe_r=("mfe_r", "mean"),
        mean_giveback_r=("peak_to_exit_giveback_r", "mean"),
    ).reset_index().sort_values("mean_r", ascending=False)
    exit_summary = closed.groupby("exit_reason").agg(
        trades=("trade_id", "size"),
        win_rate=("realized_r", lambda x: float((x > 0).mean())),
        mean_r=("realized_r", "mean"),
        mean_mfe_r=("mfe_r", "mean"),
        mean_giveback_r=("peak_to_exit_giveback_r", "mean"),
    ).reset_index()
    signaled = closed[closed.first_context_signal_date.notna()].copy()
    signal_summary = signaled.groupby([
        "first_context_signal_layer", "first_context_signal_reason"]
    ).agg(
        trades=("trade_id", "size"),
        improved=("context_exit_improvement_cash", lambda x: int((x > 0).sum())),
        worsened=("context_exit_improvement_cash", lambda x: int((x < 0).sum())),
        improvement_cash=("context_exit_improvement_cash", "sum"),
        mean_improvement_r=("context_exit_improvement_r", "mean"),
    ).reset_index()
    signal_year = signaled.assign(
        year=signaled.first_context_signal_date.astype(int) // 10000
    ).groupby("year").agg(
        trades=("trade_id", "size"),
        improved=("context_exit_improvement_cash", lambda x: int((x > 0).sum())),
        worsened=("context_exit_improvement_cash", lambda x: int((x < 0).sum())),
        improvement_cash=("context_exit_improvement_cash", "sum"),
    ).reset_index()
    return alignment_summary, exit_summary, signal_summary, signal_year


def plot(alignment_summary, signal_year, trades):
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.2), constrained_layout=True)
    ordered = alignment_summary.sort_values("mean_r")
    axes[0].barh(ordered.entry_alignment, ordered.mean_r, color="#2563eb")
    axes[0].axvline(0, color="#111827", lw=.8)
    axes[0].set_title("Entry alignment and mean R")
    axes[0].set_xlabel("Mean realized R")
    axes[1].bar(signal_year.year.astype(str), signal_year.improvement_cash,
                color=np.where(signal_year.improvement_cash >= 0,
                               "#16a34a", "#dc2626"))
    axes[1].axhline(0, color="#111827", lw=.8)
    axes[1].set_title("Context-exit diagnostic by year")
    axes[1].set_ylabel("Cash improvement vs actual exit")
    closed = trades[trades.status.eq("CLOSED")]
    scatter = axes[2].scatter(
        closed.entry_industry_relative_20d * 100,
        closed.entry_stock_industry_relative_20d * 100,
        c=closed.realized_r, cmap="RdYlGn", vmin=-2, vmax=2,
        alpha=.75, edgecolors="none")
    axes[2].axhline(0, color="#98a2b3", lw=.8)
    axes[2].axvline(0, color="#98a2b3", lw=.8)
    axes[2].set_title("Industry vs stock relative strength")
    axes[2].set_xlabel("Industry minus market, 20d (%)")
    axes[2].set_ylabel("Stock minus industry, 20d (%)")
    fig.colorbar(scatter, ax=axes[2], label="Realized R")
    fig.savefig(OUTPUT / "trade_context_summary.png", dpi=180,
                bbox_inches="tight")
    plt.close(fig)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=False)
    operations = pd.read_csv(OPERATIONS, dtype={"symbol": str})
    lineage = pd.read_csv(BACKTEST / "logical_trades.csv", dtype={"symbol": str})
    panel = SelectionPanelV2.from_research_data(
        "/Users/wjy/abu/data/csv",
        "/Users/wjy/abu/data/selection_research",
        start_date=20200101, end_date=20260930)
    config = load_market_industry_exit_config(CONFIG, "combined")
    absolute = MarketIndustryAbsoluteState(panel, config)
    features = ContextFeatures(panel, absolute)
    trades = trade_rows(panel, features, operations, lineage)
    alignment_summary, exit_summary, signal_summary, signal_year = summarize(trades)
    model_year, model_predictions = entry_model(trades)
    trades.to_csv(OUTPUT / "trade_context_detail.csv", index=False)
    alignment_summary.to_csv(OUTPUT / "entry_alignment_summary.csv", index=False)
    exit_summary.to_csv(OUTPUT / "exit_reason_summary.csv", index=False)
    signal_summary.to_csv(OUTPUT / "context_exit_summary.csv", index=False)
    signal_year.to_csv(OUTPUT / "context_exit_year_summary.csv", index=False)
    model_year.to_csv(OUTPUT / "entry_model_leave_year_out.csv", index=False)
    model_predictions.to_csv(OUTPUT / "entry_model_predictions.csv", index=False)
    plot(alignment_summary, signal_year, trades)
    closed = trades[trades.status.eq("CLOSED")]
    signaled = closed[closed.first_context_signal_date.notna()]
    summary = {
        "trades": int(len(trades)), "closed_trades": int(len(closed)),
        "win_rate": float((closed.realized_r > 0).mean()),
        "mean_realized_r": float(closed.realized_r.mean()),
        "context_signaled_trades": int(len(signaled)),
        "context_signal_independent_dates": int(
            signaled.first_context_signal_date.nunique()),
        "context_exit_improvement_cash": float(
            signaled.context_exit_improvement_cash.sum()),
        "context_exit_improved_trades": int(
            signaled.context_exit_improvement_cash.gt(0).sum()),
        "context_exit_worsened_trades": int(
            signaled.context_exit_improvement_cash.lt(0).sum()),
        "leave_year_out_auc_mean": float(model_year.auc.mean()),
        "research_only": True, "strategy_mutated": False,
        "warning": "Post-hoc attribution; context exit deltas exclude cash reuse and replacement entries.",
    }
    write_json = lambda path, payload: Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2,
                   allow_nan=False) + "\n", encoding="utf-8")
    write_json(OUTPUT / "summary.json", summary)
    report = [
        "# Alpha158 逐笔市场—行业—个股关系归因", "",
        "本报告对每笔交易分别记录入场前、持有期峰值和退出信号日前的市场、行业与个股状态。",
        "市场绝对状态只解释系统性风险，行业绝对状态解释局部风险；行业相对强弱与个股相对行业强弱解释选股来源。", "",
        "## 总览", "",
        f"- 交易：{len(trades)} 笔，其中已闭合 {len(closed)} 笔。",
        f"- 已闭合交易胜率：{summary['win_rate']:.2%}；平均 {summary['mean_realized_r']:+.3f}R。",
        f"- 市场或行业上下文在原退出前触发：{len(signaled)} 笔，分布在 {summary['context_signal_independent_dates']} 个独立日期。",
        f"- 按次日开盘退出、但不计现金再投资的诊断差额：{summary['context_exit_improvement_cash']:+,.0f} 元；改善 {summary['context_exit_improved_trades']} 笔，恶化 {summary['context_exit_worsened_trades']} 笔。",
        f"- 入场关系特征按年度留一的平均 AUC：{summary['leave_year_out_auc_mean']:.3f}。", "",
        "## 使用边界", "",
        "- 所有关系特征只使用信号日收盘前可得数据。",
        "- 峰值特征仅用于事后解释，不允许直接作为实时信号。",
        "- 上下文退出差额不是组合回测收益，因为没有重放退出后的现金和替代买入。",
        "- 若年度留一结果不稳定，不把关系模型用于过滤入场。",
        "- 只有经过完整账户回放的市场/行业覆盖层才能进入 shadow。", "",
    ]
    (OUTPUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

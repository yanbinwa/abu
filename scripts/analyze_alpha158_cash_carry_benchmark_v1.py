#!/usr/bin/env python3
"""Measure a frozen strategy path under preregistered idle-cash carry rates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_cash_carry_benchmark_v1.json"
DEFAULT_OUTPUT = Path("/Users/wjy/abu/backtests/alpha158_cash_carry_benchmark_v1_20261006")


def metrics(frame):
    capital = frame.adjusted_capital.to_numpy(dtype=float)
    dates = pd.to_datetime(frame.date.astype(str), format="%Y%m%d")
    years = max((dates.iloc[-1] - dates.iloc[0]).days / 365.0, 1 / 365)
    total = capital[-1] / capital[0] - 1
    cagr = (capital[-1] / capital[0]) ** (1 / years) - 1
    drawdown = capital / np.maximum.accumulate(capital) - 1
    return {
        "start": int(frame.date.iloc[0]), "end": int(frame.date.iloc[-1]),
        "years": years, "return_pct": total * 100, "cagr_pct": cagr * 100,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "ending_capital": float(capital[-1]),
        "cash_carry_cash": float(frame.cumulative_carry.iloc[-1]),
    }


def run_scenario(nav, annual_yield_pct, day_count):
    frame = nav.copy()
    dates = pd.to_datetime(frame.date.astype(str), format="%Y%m%d")
    carry = np.zeros(len(frame), dtype=float)
    annual = float(annual_yield_pct) / 100
    for index in range(1, len(frame)):
        days = int((dates.iloc[index] - dates.iloc[index - 1]).days)
        rate = (1 + annual) ** (days / float(day_count)) - 1
        eligible = max(float(frame.cash.iloc[index - 1]) + carry[index - 1], 0.0)
        carry[index] = carry[index - 1] + eligible * rate
    frame["annual_cash_yield_pct"] = float(annual_yield_pct)
    frame["cumulative_carry"] = carry
    frame["adjusted_cash"] = frame.cash + carry
    frame["adjusted_capital"] = frame.capital + carry
    return frame


def analyze(config, output):
    if output.exists():
        raise FileExistsError("refusing to overwrite cash carry benchmark")
    output.mkdir(parents=True)
    nav = pd.read_csv(config["source_daily_nav"])
    scenarios, curves = [], []
    for annual in config["annual_cash_yield_scenarios_pct"]:
        curve = run_scenario(nav, annual, config["day_count"])
        item = {"annual_cash_yield_pct": float(annual), **metrics(curve)}
        scenarios.append(item)
        curves.append(curve[["date", "annual_cash_yield_pct", "cumulative_carry",
                             "adjusted_cash", "adjusted_capital"]])
    result = pd.DataFrame(scenarios)
    baseline = result[result.annual_cash_yield_pct.eq(0)].iloc[0]
    result["incremental_return_pp"] = result.return_pct - baseline.return_pct
    result["incremental_cagr_pp"] = result.cagr_pct - baseline.cagr_pct
    result["drawdown_improvement_pp"] = (
        result.max_drawdown_pct - baseline.max_drawdown_pct)
    result.to_csv(output / "scenario_metrics.csv", index=False)
    pd.concat(curves, ignore_index=True).to_csv(output / "scenario_daily_nav.csv", index=False)
    exposure = nav.stocks / nav.capital
    cash_ratio = nav.cash / nav.capital
    summary = {
        "source_daily_nav": config["source_daily_nav"],
        "average_exposure_pct": float(exposure.mean() * 100),
        "average_cash_ratio_pct": float(cash_ratio.mean() * 100),
        "minimum_cash_ratio_pct": float(cash_ratio.min() * 100),
        "maximum_cash_ratio_pct": float(cash_ratio.max() * 100),
        "scenario_count": len(result),
        "decision": "CASH_MANAGEMENT_BENCHMARK_ONLY",
        "limitations": [
            "No specific cash-management product, fees, taxes, settlement or liquidity is modeled.",
            "Trading orders and risk sizing are held fixed, so accrued carry is not redeployed.",
            "The uplift is financing income caused by idle cash and is not selection alpha.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(
        summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Alpha158 闲置现金收益基准", "",
        "本分析固定原有持仓、订单、成交和风控路径，只按相邻交易日之间的自然日数，对上一交易日现金及已累计利息计息。利率场景在计算前固定为 0%、1.0%、1.5%、2.0%，没有根据结果选利率。", "",
        f"原路径平均股票仓位为 {summary['average_exposure_pct']:.2f}%，平均现金比例为 {summary['average_cash_ratio_pct']:.2f}%。因此，现金管理对组合年化收益的影响会比较明显，但这部分属于现金收益，不是选股能力提升。", "",
        "| 现金年化假设 | 累计收益 | 年化收益 | 最大回撤幅度 | 期末现金收益 | 年化增量 |", "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result.itertuples():
        lines.append(f"| {row.annual_cash_yield_pct:.1f}% | {row.return_pct:.2f}% | {row.cagr_pct:.2f}% | {abs(row.max_drawdown_pct):.2f}% | {row.cash_carry_cash:,.2f} | {row.incremental_cagr_pp:+.2f} pp |")
    lines += ["", "## 解释与使用边界", "",
              "该表回答的是大量现金闲置时，低风险现金工具可能贡献多少收益。它没有建模具体货币基金、国债逆回购或 ETF 的申赎费用、税费、结算、可用时间及流动性，也没有把新增利息重新投入股票。因此暂不直接接入模拟盘。下一步如要落地，应先选定可交易工具，再用真实历史净值和交易规则做独立执行回放。", "",
              "现金收益可改善组合总收益和轻微缓冲回撤，但不能用于证明 Alpha158 排名更有效；策略报告应始终同时列出股票策略净值与含现金管理净值。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return summary, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    summary, result = analyze(config, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()

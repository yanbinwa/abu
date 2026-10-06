#!/usr/bin/env python3
"""Build forward-only orthogonal signal snapshots without optimizing returns."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_orthogonal_signal_shadow_v1.json"
DEFAULT_OUTPUT = Path("/Users/wjy/abu/shadow/alpha158_orthogonal_signals_v1")


def _symmetric_growth(current, previous):
    if current is None or previous is None:
        return np.nan
    denominator = abs(current) + abs(previous)
    return 0.0 if denominator == 0 else 2 * (current - previous) / denominator


def _ratio(numerator, denominator, clip=None):
    if numerator is None or denominator is None or not np.isfinite(denominator) or denominator == 0:
        return np.nan
    value = float(numerator) / float(denominator)
    if clip is not None:
        value = float(np.clip(value, clip[0], clip[1]))
    return value


def _features(symbol, items, available_from):
    values = {}
    for item in items:
        values[(str(item["report_period"])[:10], str(item["raw_field"]))] = float(item["raw_value"])
    periods = sorted({key[0] for key in values})
    def latest_with(fields):
        matches = [period for period in periods if all((period, field) in values for field in fields)]
        return matches[-1] if matches else None
    quality_fields = ["PARENT_NETPROFIT", "NETCASH_OPERATE", "TOTAL_ASSETS",
                      "TOTAL_LIABILITIES", "TOTAL_PARENT_EQUITY"]
    quality_period = latest_with(quality_fields)
    margin_period = latest_with(["TOTAL_OPERATE_INCOME", "OPERATE_COST"])
    capex_period = latest_with(["NETCASH_OPERATE", "CONSTRUCT_LONG_ASSET"])
    growth_period = latest_with(["TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT"])
    previous_period = None
    if growth_period:
        target = date.fromisoformat(growth_period).replace(
            year=date.fromisoformat(growth_period).year - 1).isoformat()
        if ((target, "TOTAL_OPERATE_INCOME") in values and
                (target, "PARENT_NETPROFIT") in values):
            previous_period = target
    def get(period, field):
        return values.get((period, field)) if period else None
    net_profit = get(quality_period, "PARENT_NETPROFIT")
    cash_flow = get(quality_period, "NETCASH_OPERATE")
    assets = get(quality_period, "TOTAL_ASSETS")
    liabilities = get(quality_period, "TOTAL_LIABILITIES")
    equity = get(quality_period, "TOTAL_PARENT_EQUITY")
    revenue = get(margin_period, "TOTAL_OPERATE_INCOME")
    operating_cost = get(margin_period, "OPERATE_COST")
    capex = get(capex_period, "CONSTRUCT_LONG_ASSET")
    capex_cash = get(capex_period, "NETCASH_OPERATE")
    current_revenue = get(growth_period, "TOTAL_OPERATE_INCOME")
    prior_revenue = get(previous_period, "TOTAL_OPERATE_INCOME")
    current_profit = get(growth_period, "PARENT_NETPROFIT")
    prior_profit = get(previous_period, "PARENT_NETPROFIT")
    return {
        "symbol": symbol, "available_from": available_from,
        "quality_report_period": quality_period,
        "margin_report_period": margin_period,
        "growth_report_period": growth_period,
        "growth_comparison_period": previous_period,
        "capital_report_period": capex_period,
        "return_on_parent_equity_cumulative": _ratio(net_profit, equity),
        "operating_margin_cumulative": _ratio(
            None if revenue is None or operating_cost is None else revenue - operating_cost,
            revenue),
        "operating_cash_conversion_cumulative": _ratio(
            cash_flow, None if net_profit is None else abs(net_profit), (-5, 5)),
        "liability_to_asset": _ratio(liabilities, assets),
        "revenue_yoy_symmetric": _symmetric_growth(current_revenue, prior_revenue),
        "parent_net_profit_yoy_symmetric": _symmetric_growth(current_profit, prior_profit),
        "capital_expenditure_to_operating_cash": _ratio(
            capex, None if capex_cash is None else abs(capex_cash), (0, 10)),
    }


def build_financial_snapshot(facts_path, available_from):
    output, current_symbol, items = [], None, []
    with Path(facts_path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            item = json.loads(line)
            symbol = str(item["symbol"])
            if current_symbol is not None and symbol != current_symbol:
                output.append(_features(current_symbol, items, available_from))
                items = []
            current_symbol = symbol
            items.append(item)
    if current_symbol is not None:
        output.append(_features(current_symbol, items, available_from))
    return pd.DataFrame(output)


def build(config, output):
    if output.exists():
        raise FileExistsError("refusing to overwrite orthogonal shadow registry")
    output.mkdir(parents=True)
    quality_root = Path(config["fundamental_quality_root"])
    quality = json.loads((quality_root / "quality_report.json").read_text(
        encoding="utf-8"))
    if quality["forward_snapshot_status"] != "ELIGIBLE_FOR_FORWARD_SHADOW_AFTER_INGESTION":
        raise ValueError("fundamental snapshot has not passed forward gates")
    available_from = quality["forward_snapshot_not_before"]
    merged = Path(config["merged_fundamental_root"])
    financial = build_financial_snapshot(
        merged / "structured_fundamental_facts.jsonl", available_from)
    financial.to_csv(output / "financial_quality_growth_snapshot.csv.gz",
                     index=False, compression={"method": "gzip", "compresslevel": 3})
    industry_all = pd.read_csv(config["industry_strength_file"])
    latest_industry_date = int(industry_all.trade_date.max())
    industry_columns = ["trade_date", "industry_id", "industry_name", "available_at",
                        "coverage_ratio"] + list(config["industry_features"])
    industry = industry_all[industry_all.trade_date.eq(latest_industry_date)][industry_columns]
    industry.to_csv(output / "industry_context_snapshot.csv", index=False)
    coverage = {}
    for field in config["financial_features"]:
        coverage[field] = float(financial[field].notna().mean())
    complete = financial[list(config["financial_features"])].notna().all(axis=1)
    report = {
        "version": config["version"],
        "created_from_observed_returns": False,
        "return_or_ic_optimization_performed": False,
        "fundamental_symbols": int(len(financial)),
        "complete_financial_feature_symbols": int(complete.sum()),
        "complete_financial_feature_coverage": float(complete.mean()),
        "financial_feature_coverage": coverage,
        "fundamental_available_from": available_from,
        "historical_financial_backtest_allowed": False,
        "industry_snapshot_date": latest_industry_date,
        "industry_count": int(len(industry)),
        "families": config["families"],
        "combined_score_created": False,
        "trade_or_filter_enabled": False,
        "next_action": (
            "Log these fields beside future Alpha158 candidates. Evaluate coverage, "
            "cross-sectional correlation and predeclared forward outcomes only after "
            "labels mature; never reconstruct pre-ingestion financial scores."),
    }
    (output / "readiness_report.json").write_text(json.dumps(
        report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Alpha158 正交信号前瞻注册", "",
             "本版本只生成观察字段，不合成总分、不筛选候选、不下单，也不使用收益或 IC 选择公式。", "",
             "| 信号族 | 状态 | 处理 |", "|---|---|---|",
             "| 行业环境 | 前瞻 shadow | 记录行业超额强度、宽度与量能；历史年度稳定性未通过，不作为交易过滤 |",
             "| 财务质量与增长 | 前瞻 shadow | 从本次采集完成后记录质量、增长和资本开支原始特征；禁止回填历史 |",
             "| 公告意外 | 阻塞 | 当前没有一致预期基准且历史修订链不完整，不能构造可靠 surprise |", "",
             f"财务快照覆盖 {len(financial)} 只证券；全部七个预登记特征同时可用的证券为 {int(complete.sum())} 只（{complete.mean():.2%}）。其可用时间不得早于 `{available_from}`。", "",
             "下一步只在未来 Alpha158 候选旁记录这些字段。等标签自然成熟后，先检查覆盖率和与现有 Alpha158 分数的相关性，再按预登记规则评估增量；没有新的前瞻样本前不建立组合权重。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    print(json.dumps(build(config, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

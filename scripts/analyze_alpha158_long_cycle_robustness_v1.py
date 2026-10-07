#!/usr/bin/env python3
"""Diagnose long-cycle stability without selecting or tuning a new strategy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FAMILIES = (
    "regression_trend", "price_position", "volume_structure",
    "price_volume_persistence", "kbar_shape", "vwap_price",
    "residual_overheat",
)
ERAS = {
    "2015_2019": (20150101, 20191231),
    "2020_2022": (20200101, 20221231),
    "2023_2026": (20230101, 20260930),
    "2015_2026": (20150101, 20260930),
}
BUCKETS = ((1, 10, "top_1_10"), (11, 20, "top_11_20"),
           (21, 30, "top_21_30"), (31, 50, "top_31_50"))


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def daily_spearman(frame, score):
    work = frame[["signal_asof", "target_rank", score]].dropna().copy()
    work["x"] = work.groupby("signal_asof", sort=False)[score].rank(
        method="average", pct=True) - .5
    work["y"] = work.target_rank.astype(float)
    work["xy"] = work.x * work.y
    work["x2"] = work.x * work.x
    work["y2"] = work.y * work.y
    grouped = work.groupby("signal_asof", sort=True).agg(
        n=("x", "size"), sx=("x", "sum"), sy=("y", "sum"),
        sxy=("xy", "sum"), sx2=("x2", "sum"), sy2=("y2", "sum"))
    numerator = grouped.sxy - grouped.sx * grouped.sy / grouped.n
    denominator = np.sqrt(
        (grouped.sx2 - grouped.sx ** 2 / grouped.n) *
        (grouped.sy2 - grouped.sy ** 2 / grouped.n))
    result = numerator / denominator.replace(0, np.nan)
    return result.rename("rank_ic").reset_index()


def block_mean_interval(values, seed, block=20, replicates=2000):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < block:
        return [None, None]
    rng = np.random.default_rng(seed)
    blocks = int(np.ceil(len(values) / block))
    starts = rng.integers(0, len(values), size=(replicates, blocks, 1))
    indices = (starts + np.arange(block)) % len(values)
    samples = values[indices.reshape(replicates, -1)[:, :len(values)]]
    means = samples.mean(axis=1)
    return [float(np.quantile(means, .025)),
            float(np.quantile(means, .975))]


def score_diagnostic(frame, score, model, seed):
    daily = daily_spearman(frame, score)
    daily["year"] = daily.signal_asof.astype(int) // 10000
    annual = daily.groupby("year", as_index=False).rank_ic.mean()
    annual["model"] = model
    era_rows = []
    for offset, (era, (start, end)) in enumerate(ERAS.items()):
        part = daily[daily.signal_asof.between(start, end)]
        years = annual[annual.year.between(start // 10000, end // 10000)]
        interval = block_mean_interval(
            part.rank_ic, seed + offset * 100, block=20, replicates=2000)
        mean = float(part.rank_ic.mean())
        std = float(part.rank_ic.std(ddof=1))
        era_rows.append({
            "model": model, "era": era, "sessions": int(len(part)),
            "mean_rank_ic": mean,
            "rank_ic_ir_annualized": (
                float(mean / std * np.sqrt(242)) if std > 0 else None),
            "positive_session_fraction": float((part.rank_ic > 0).mean()),
            "ci95_low": interval[0], "ci95_high": interval[1],
            "positive_years": int((years.rank_ic > 0).sum()),
            "years": int(len(years)),
            "minimum_annual_rank_ic": float(years.rank_ic.min()),
        })
    ranked = frame[["signal_asof", "excess_return_20d", score]].dropna().copy()
    ranked["ordinal_rank"] = ranked.groupby(
        "signal_asof", sort=False)[score].rank(method="first", ascending=False)
    bucket_rows = []
    for low, high, bucket in BUCKETS:
        selected = ranked[ranked.ordinal_rank.between(low, high)]
        per_date = selected.groupby(
            "signal_asof", sort=True).excess_return_20d.mean().reset_index()
        for era, (start, end) in ERAS.items():
            part = per_date[per_date.signal_asof.between(start, end)]
            bucket_rows.append({
                "model": model, "bucket": bucket, "era": era,
                "dates": int(len(part)),
                "mean_forward_20d_excess_pct": float(
                    part.excess_return_20d.mean() * 100),
                "median_forward_20d_excess_pct": float(
                    part.excess_return_20d.median() * 100),
                "positive_date_fraction": float(
                    (part.excess_return_20d > 0).mean()),
            })
    return pd.DataFrame(era_rows), annual, pd.DataFrame(bucket_rows), daily


def segment_account(curve, start, end):
    work = curve[curve.date.between(start, end)].copy()
    prior = work.capital.shift(1)
    daily_return = work.capital / prior - 1
    lag_exposure = work.exposure.shift(1)
    valid = daily_return.notna() & lag_exposure.gt(.005)
    start_date = pd.Timestamp(str(int(work.date.iloc[0])))
    end_date = pd.Timestamp(str(int(work.date.iloc[-1])))
    years = max((end_date - start_date).days / 365.25, 1 / 365.25)
    nav = work.capital.to_numpy(dtype=float)
    return {
        "start": int(work.date.iloc[0]), "end": int(work.date.iloc[-1]),
        "sessions": int(len(work)),
        "return_pct": float((nav[-1] / nav[0] - 1) * 100),
        "cagr_pct": float(((nav[-1] / nav[0]) ** (1 / years) - 1) * 100),
        "max_drawdown_pct": float(
            (nav / np.maximum.accumulate(nav) - 1).min() * 100),
        "average_exposure_pct": float(work.exposure.mean() * 100),
        "active_session_fraction": float(valid.mean()),
        "exposure_normalized_arithmetic_return_pct_pa": float(
            242 * daily_return[valid].sum() / lag_exposure[valid].sum() * 100),
    }


def trade_concentration(directory):
    dispositions = pd.read_csv(directory / "lot_dispositions.csv")
    trades = pd.read_csv(directory / "logical_trades.csv")
    realized = dispositions.groupby(
        ["trade_id", "symbol"], as_index=False).agg(
            realized_pnl_cash=("realized_pnl_cash", "sum"),
            exit_date=("fill_date", "max"), exit_reason=("exit_reason", "last"))
    realized = realized.merge(
        trades[["trade_id", "opened_at"]], on="trade_id", how="left")
    realized["entry_year"] = realized.opened_at // 10000
    realized["board"] = np.where(
        realized.symbol.str[2:5].isin(["300", "301"]),
        "chinext", "shenzhen_main_sme")
    positive = realized[realized.realized_pnl_cash > 0].sort_values(
        "realized_pnl_cash", ascending=False)
    total_positive = float(positive.realized_pnl_cash.sum())
    net = float(realized.realized_pnl_cash.sum())
    summary = {
        "closed_trades": int(len(realized)),
        "win_rate_pct": float((realized.realized_pnl_cash > 0).mean() * 100),
        "net_realized_pnl_cash": net,
        "top5_share_of_positive_profit_pct": float(
            positive.head(5).realized_pnl_cash.sum() / total_positive * 100),
        "top10_share_of_positive_profit_pct": float(
            positive.head(10).realized_pnl_cash.sum() / total_positive * 100),
        "top10_share_of_net_profit_pct": float(
            positive.head(10).realized_pnl_cash.sum() / net * 100),
    }
    board = realized.groupby("board").agg(
        trades=("trade_id", "size"),
        win_rate=("realized_pnl_cash", lambda x: float((x > 0).mean())),
        realized_pnl_cash=("realized_pnl_cash", "sum")).reset_index()
    board["pnl_share_pct"] = board.realized_pnl_cash / net * 100
    return realized, positive.head(20), board, summary


def render_report(era, buckets, account, trade_summary, board, annual):
    stable = era[(era.era != "2015_2026")].groupby("model").agg(
        positive_eras=("mean_rank_ic", lambda x: int((x > 0).sum())),
        min_era_ic=("mean_rank_ic", "min"),
        min_ci_low=("ci95_low", "min")).sort_values(
            ["positive_eras", "min_era_ic"], ascending=False)
    all_period = era[era.era.eq("2015_2026")].set_index("model")
    top = buckets[(buckets.era.eq("2015_2026")) &
                  (buckets.bucket.eq("top_1_10"))].set_index("model")
    lines = ["# Alpha158 2015–2026 长周期稳健性诊断", "",
             "本报告只诊断冻结样本外预测与账户结果，不选择因子、不调整权重、不产生新策略。", "",
             "## 因子族跨时期稳定性", "",
             "|模型|全期Rank IC|全期95%区间|三个时期为正数|最差时期IC|Top10未来20日超额|",
             "|---|---:|---:|---:|---:|---:|"]
    for model in stable.index:
        whole = all_period.loc[model]
        lines.append(
            f"|{model}|{whole.mean_rank_ic:+.4f}|"
            f"[{whole.ci95_low:+.4f}, {whole.ci95_high:+.4f}]|"
            f"{int(stable.loc[model, 'positive_eras'])}/3|"
            f"{stable.loc[model, 'min_era_ic']:+.4f}|"
            f"{top.loc[model, 'mean_forward_20d_excess_pct']:+.2f}%|")
    lines += ["", "## 冻结账户", "",
              "|时期|累计收益|CAGR|最大回撤|平均仓位|单位暴露算术收益年率*|",
              "|---|---:|---:|---:|---:|---:|"]
    for row in account.itertuples(index=False):
        lines.append(
            f"|{row.era}|{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|{row.average_exposure_pct:.2f}%|"
            f"{row.exposure_normalized_arithmetic_return_pct_pa:+.2f}%|")
    lines += ["", "\* 单位暴露算术收益用于诊断资本利用率，不是可直接实现的复利年化。", "",
              "## 交易集中度", "",
              f"- 已平仓：{trade_summary['closed_trades']}笔；胜率：{trade_summary['win_rate_pct']:.2f}%",
              f"- 前5笔赢家占全部正收益：{trade_summary['top5_share_of_positive_profit_pct']:.2f}%",
              f"- 前10笔赢家占全部正收益：{trade_summary['top10_share_of_positive_profit_pct']:.2f}%",
              f"- 前10笔赢家/净已实现收益：{trade_summary['top10_share_of_net_profit_pct']:.2f}%", "",
              "## 数据边界", "",
              "历史池仅含深圳A股；供应商数据是回溯下载，不能声称严格同日PIT。",
              "行业历史元数据没有通过完整覆盖审计，因此本轮不依据行业归因选择因子。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007"))
    parser.add_argument("--output", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_long_cycle_diagnostic_v1_20261007"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    era_frames, annual_frames, bucket_frames, daily_frames = [], [], [], []
    for offset, family in enumerate(FAMILIES):
        print("ANALYZE " + family, flush=True)
        frame = pd.read_csv(
            args.source / "families" / family / "oos_predictions.csv.gz",
            usecols=["signal_asof", "target_rank", "excess_return_20d",
                     "ridge_score", "candidate_score"])
        frame = frame[frame.signal_asof.between(20150101, 20260930)]
        era, annual, buckets, daily = score_diagnostic(
            frame, "candidate_score", family, 20261007 + offset * 1000)
        era_frames.append(era); annual_frames.append(annual)
        bucket_frames.append(buckets); daily["model"] = family
        daily_frames.append(daily)
        if offset == 0:
            era, annual, buckets, daily = score_diagnostic(
                frame, "ridge_score", "ridge_baseline", 20269999)
            era_frames.append(era); annual_frames.append(annual)
            bucket_frames.append(buckets); daily["model"] = "ridge_baseline"
            daily_frames.append(daily)
        del frame
    ensemble = pd.read_csv(
        args.source / "ensemble_oos_predictions.csv.gz",
        usecols=["signal_asof", "target_rank", "excess_return_20d",
                 "all_mean_rank"])
    era, annual, buckets, daily = score_diagnostic(
        ensemble, "all_mean_rank", "all_mean_rank", 20268888)
    era_frames.append(era); annual_frames.append(annual)
    bucket_frames.append(buckets); daily["model"] = "all_mean_rank"
    daily_frames.append(daily)
    era = pd.concat(era_frames, ignore_index=True)
    annual = pd.concat(annual_frames, ignore_index=True)
    buckets = pd.concat(bucket_frames, ignore_index=True)
    daily = pd.concat(daily_frames, ignore_index=True)
    curve = pd.read_csv(args.source / "all_mean_rank_25bp/daily_nav.csv")
    account_rows = []
    for name, bounds in ERAS.items():
        metrics = segment_account(curve, *bounds)
        metrics["era"] = name
        account_rows.append(metrics)
    account = pd.DataFrame(account_rows)
    realized, winners, board, trade_summary = trade_concentration(
        args.source / "all_mean_rank_25bp")
    era.to_csv(args.output / "factor_era_rank_ic.csv", index=False)
    annual.to_csv(args.output / "factor_annual_rank_ic.csv", index=False)
    buckets.to_csv(args.output / "factor_rank_bucket_returns.csv", index=False)
    daily.to_csv(args.output / "factor_daily_rank_ic.csv", index=False)
    account.to_csv(args.output / "account_era_metrics.csv", index=False)
    realized.to_csv(args.output / "trade_realized_pnl.csv", index=False)
    winners.to_csv(args.output / "top20_winners.csv", index=False)
    board.to_csv(args.output / "board_attribution.csv", index=False)
    write_json(args.output / "trade_concentration.json", trade_summary)
    (args.output / "REPORT.md").write_text(
        render_report(era, buckets, account, trade_summary, board, annual),
        encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE", "models": int(era.model.nunique()),
        "parameter_search_performed": False,
        "new_strategy_selected": False,
    })
    print(args.output / "REPORT.md", flush=True)


if __name__ == "__main__":
    main()

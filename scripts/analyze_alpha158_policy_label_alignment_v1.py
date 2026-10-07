#!/usr/bin/env python3
"""Compare the frozen 20-session target with the event-exit target."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_mean_ml_combiner_v1_stop_invalidated_20261007/"
    "oos_predictions.csv.gz")
DEFAULT_LABELS = Path(
    "/Users/wjy/abu/data/selection_research_2015_v1/"
    "alpha158_policy_labels_v1_20261007/labels.csv.gz")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_policy_label_alignment_v1_20261007")


def _safe_corr(left, right):
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    valid = np.isfinite(left) & np.isfinite(right)
    if valid.sum() < 3:
        return np.nan
    left = left[valid]
    right = right[valid]
    if np.ptp(left) <= 1e-15 or np.ptp(right) <= 1e-15:
        return np.nan
    return float(np.corrcoef(left, right)[0, 1])


def summarize_day(frame):
    """Return date-level diagnostics; no row may influence another date."""
    frame = frame.sort_values(["symbol"], kind="mergesort")
    valid = frame.event_target_rank.notna()
    eligible = frame.loc[valid]
    result = {
        "signal_asof": int(frame.signal_asof.iloc[0]),
        "rows": int(len(frame)),
        "event_target_rows": int(valid.sum()),
        "old_event_label_corr": _safe_corr(
            eligible.old_target_rank, eligible.event_target_rank),
    }
    for model in ("a0", "a1"):
        score = "{}_score".format(model)
        result["{}_old_ic".format(model)] = _safe_corr(
            frame[score], frame.old_target_rank)
        result["{}_event_ic".format(model)] = _safe_corr(
            eligible[score], eligible.event_target_rank)
        top = eligible.nlargest(min(10, len(eligible)), score)
        result["{}_top10_event_r".format(model)] = (
            float(top.event_path_r_60d.mean()) if len(top) else np.nan)
        result["{}_top10_event_win_rate".format(model)] = (
            float(top.event_path_r_60d.gt(0).mean()) if len(top) else np.nan)
    a0 = set(eligible.nlargest(min(10, len(eligible)), "a0_score").symbol)
    a1 = set(eligible.nlargest(min(10, len(eligible)), "a1_score").symbol)
    result["a0_a1_top10_overlap"] = len(a0 & a1)/max(1, min(10, len(eligible)))
    return result


def _aggregate(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"dates": 0, "mean": None, "median": None,
                "positive_rate": None}
    return {
        "dates": int(len(values)),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "positive_rate": float((values > 0).mean()),
    }


def _write_markdown(output, report):
    summary = report["date_level_summary"]
    lines = [
        "# Alpha158 固定20日标签与事件退出标签对齐诊断", "",
        "- 状态：`{}`".format(report["status"]),
        "- 对齐候选：`{:,}` 行，`{:,}` 个信号日。".format(
            report["aligned_rows"], report["signal_dates"]),
        "- 日均旧/新标签相关性：`{:.4f}`。".format(
            summary["old_event_label_corr"]["mean"]),
        "", "## 日期等权结果", "",
        "| 指标 | 均值 | 中位数 | 正值日期占比 |", "|---|---:|---:|---:|",
    ]
    for name in (
            "old_event_label_corr", "a0_old_ic", "a0_event_ic",
            "a1_old_ic", "a1_event_ic", "a0_top10_event_r",
            "a1_top10_event_r", "a0_top10_event_win_rate",
            "a1_top10_event_win_rate", "a0_a1_top10_overlap"):
        item = summary[name]
        lines.append("| {} | {:.4f} | {:.4f} | {:.2%} |".format(
            name, item["mean"], item["median"], item["positive_rate"]))
    lines += [
        "", "本报告仅诊断标签关系和既有分数对新标签的解释力；未重新训练模型，",
        "不构成候选策略接纳或正式策略替换证据。", "",
    ]
    (output/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def run(args):
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    prediction_columns = [
        "signal_asof", "symbol", "column", "target_rank", "a0_score",
        "a1_score"]
    label_columns = [
        "signal_asof", "symbol", "column", "target_rank",
        "event_path_r_60d"]
    pred_reader = pd.read_csv(
        args.predictions, usecols=prediction_columns,
        chunksize=args.chunksize)
    label_reader = pd.read_csv(
        args.labels, usecols=label_columns, chunksize=args.chunksize)
    records = []
    carry = None
    aligned_rows = 0
    for predictions, labels in zip(pred_reader, label_reader):
        predictions = predictions.iloc[:len(labels)].reset_index(drop=True)
        labels = labels.reset_index(drop=True)
        keys = ["signal_asof", "symbol", "column"]
        if not predictions[keys].equals(labels[keys]):
            raise ValueError("prediction and policy-label keys are not aligned")
        frame = predictions.rename(columns={"target_rank": "old_target_rank"})
        frame["event_target_rank"] = labels.target_rank
        frame["event_path_r_60d"] = labels.event_path_r_60d
        aligned_rows += len(frame)
        if carry is not None:
            frame = pd.concat([carry, frame], ignore_index=True)
        last_date = int(frame.signal_asof.iloc[-1])
        carry = frame.loc[frame.signal_asof.eq(last_date)].copy()
        complete = frame.loc[~frame.signal_asof.eq(last_date)]
        records.extend(summarize_day(group) for _, group in
                       complete.groupby("signal_asof", sort=False))
    if carry is not None and len(carry):
        records.append(summarize_day(carry))
    daily = pd.DataFrame(records).sort_values("signal_asof").reset_index(drop=True)
    if int(daily.rows.sum()) != aligned_rows:
        raise ValueError("date aggregation lost aligned rows")
    metrics = [column for column in daily.columns if column not in (
        "signal_asof", "rows", "event_target_rows")]
    summary = {name: _aggregate(daily[name]) for name in metrics}
    yearly = {}
    for year, group in daily.groupby(daily.signal_asof//10000):
        yearly[str(int(year))] = {
            name: _aggregate(group[name])["mean"] for name in metrics}
    report = {
        "analysis_id": "alpha158_policy_label_alignment_v1",
        "status": "COMPLETE_DIAGNOSTIC_ONLY",
        "predictions": str(args.predictions),
        "labels": str(args.labels),
        "aligned_rows": int(aligned_rows),
        "signal_dates": int(len(daily)),
        "date_level_summary": summary,
        "yearly_date_equal_means": yearly,
        "decision": "NO_MODEL_TRAINING_NO_STRATEGY_CHANGE",
    }
    args.output_dir.mkdir(parents=True)
    daily.to_csv(args.output_dir/"daily_metrics.csv", index=False)
    (args.output_dir/"report.json").write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False)+"\n", encoding="utf-8")
    _write_markdown(args.output_dir, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

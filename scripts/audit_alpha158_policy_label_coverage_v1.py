#!/usr/bin/env python3
"""Generate and audit full OOS-candidate policy labels without model fitting."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
from collections import Counter, defaultdict
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


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_mean_ml_combiner_v1_stop_invalidated_20261007/"
    "oos_predictions.csv.gz")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/data/selection_research_2015_v1/"
    "alpha158_policy_labels_v1_20261007")
EVENT_REASONS = {"INITIAL_STOP", "TRAILING_STOP", "STAGNATION"}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CoverageAccumulator(object):

    def __init__(self):
        self.rows = 0
        self.signal_dates = set()
        self.entry_reason = Counter()
        self.event_reason = Counter()
        self.valid_targets = 0
        self.executable = 0
        self.yearly = defaultdict(lambda: {
            "rows": 0, "executable": 0, "valid_targets": 0,
            "time_mark_60": 0})

    def add(self, frame):
        if frame.empty:
            return
        self.rows += len(frame)
        date = int(frame.signal_asof.iloc[0])
        self.signal_dates.add(date)
        self.entry_reason.update(frame.entry_reason.fillna("MISSING"))
        self.event_reason.update(
            frame.event_path_reason_60d.fillna("NO_EVENT_LABEL"))
        executable = frame.entry_executable.astype(bool)
        valid = frame.event_path_r_60d.notna()
        self.executable += int(executable.sum())
        self.valid_targets += int(valid.sum())
        year = str(date//10000)
        self.yearly[year]["rows"] += len(frame)
        self.yearly[year]["executable"] += int(executable.sum())
        self.yearly[year]["valid_targets"] += int(valid.sum())
        self.yearly[year]["time_mark_60"] += int(
            frame.event_path_reason_60d.eq("TIME_MARK_60").sum())

    def payload(self):
        observed = sum(self.event_reason.get(reason, 0)
                       for reason in EVENT_REASONS)
        time_mark = self.event_reason.get("TIME_MARK_60", 0)
        return {
            "rows": int(self.rows),
            "signal_dates": int(len(self.signal_dates)),
            "entry_executable_rows": int(self.executable),
            "entry_executable_rate": self.executable/self.rows,
            "valid_target_rows": int(self.valid_targets),
            "valid_target_rate_all_rows": self.valid_targets/self.rows,
            "valid_target_rate_executable": (
                self.valid_targets/self.executable if self.executable else 0.0),
            "observed_event_rows": int(observed),
            "observed_event_rate_executable": (
                observed/self.executable if self.executable else 0.0),
            "time_mark_60_rows": int(time_mark),
            "time_mark_60_rate_executable": (
                time_mark/self.executable if self.executable else 0.0),
            "entry_reason_counts": dict(sorted(self.entry_reason.items())),
            "event_reason_counts": dict(sorted(self.event_reason.items())),
            "yearly": dict(sorted(self.yearly.items())),
        }


def _write_markdown(output, report):
    coverage = report["coverage"]
    lines = [
        "# Alpha158事件退出标签全候选覆盖审计", "",
        "- 状态：`{}`".format(report["status"]),
        "- 来源候选：`{:,}` 行，成熟标签：`{:,}` 行。".format(
            report["source_rows"], coverage["rows"]),
        "- 信号日：`{:,}` 个；因固定61日成熟边界排除尾部 `{:,.0f}` 行。".format(
            coverage["signal_dates"], report["excluded_immature_rows"]),
        "- 入场可执行率：`{:.2%}`；可执行样本有效目标覆盖率：`{:.2%}`。".format(
            coverage["entry_executable_rate"],
            coverage["valid_target_rate_executable"]),
        "- 60日完全观察事件占可执行样本：`{:.2%}`；`TIME_MARK_60`占比：`{:.2%}`。".format(
            coverage["observed_event_rate_executable"],
            coverage["time_mark_60_rate_executable"]),
        "", "## 入场结果", "",
        "| 原因 | 行数 |", "|---|---:|",
    ]
    for reason, count in coverage["entry_reason_counts"].items():
        lines.append("| {} | {:,} |".format(reason, count))
    lines += ["", "## 事件路径结果", "", "| 原因 | 行数 |", "|---|---:|"]
    for reason, count in coverage["event_reason_counts"].items():
        lines.append("| {} | {:,} |".format(reason, count))
    lines += ["", "该审计只确认标签覆盖与PIT成熟边界，不构成模型或策略收益证据。", ""]
    (output/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def run(args):
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    source_hash = file_sha256(args.source_predictions)
    keys = pd.read_csv(
        args.source_predictions, usecols=["signal_asof", "column"],
        dtype={"signal_asof": np.int32, "column": np.int32})
    if keys.duplicated(["signal_asof", "column"]).any():
        raise ValueError("source OOS candidate keys are not unique")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20110101, end_date=20260930)
    strategy = load_alpha158_lite_config(args.strategy_config)
    label_config = load_alpha158_policy_label_config(args.label_config)
    builder = Alpha158PolicyLabelBuilder(panel, strategy, label_config)
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    mature_dates = {
        date for date in keys.signal_asof.unique()
        if date in date_index and date_index[int(date)]+61 < len(panel.dates)}
    immature = ~keys.signal_asof.isin(mature_dates)
    excluded_rows = int(immature.sum())
    excluded_dates = int(keys.loc[immature, "signal_asof"].nunique())
    keys = keys.loc[~immature].copy()
    args.output_dir.mkdir(parents=True)
    target = args.output_dir/"labels.csv.gz"
    accumulator = CoverageAccumulator()
    with gzip.GzipFile(target, "wb", compresslevel=3, mtime=0) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as handle:
            first = True
            grouped = keys.groupby("signal_asof", sort=True)
            for number, (date, group) in enumerate(grouped, start=1):
                labels = builder.build_day(
                    date_index[int(date)], group.column.to_numpy(dtype=int))
                if len(labels) != len(group) or set(labels.column) != set(group.column):
                    raise ValueError("label keys differ from source candidate keys")
                labels["target_rank"] = labels.event_path_r_60d.rank(
                    method="average", pct=True)-.5
                accumulator.add(labels)
                labels.to_csv(handle, index=False, header=first)
                first = False
                if number % 100 == 0:
                    print(json.dumps({
                        "processed_signal_dates": number,
                        "processed_rows": accumulator.rows,
                    }), flush=True)
    coverage = accumulator.payload()
    output_rows = sum(len(chunk) for chunk in pd.read_csv(
        target, usecols=["signal_asof"], chunksize=500_000))
    if output_rows != coverage["rows"] or coverage["rows"] != len(keys):
        raise ValueError("label output is incomplete")
    status = "PASS" if (
        coverage["valid_target_rate_executable"] >= 0.995 and
        coverage["rows"] == len(keys)) else "FAIL"
    report = {
        "audit_id": "alpha158_policy_label_coverage_v1",
        "status": status,
        "source_predictions": str(args.source_predictions),
        "source_predictions_sha256": source_hash,
        "source_rows": int(len(keys)+excluded_rows),
        "excluded_immature_rows": excluded_rows,
        "excluded_immature_signal_dates": excluded_dates,
        "label_version": label_config.label_version,
        "label_config_sha256": builder.config_sha256,
        "labels_sha256": file_sha256(target),
        "coverage": coverage,
        "decision": "COVERAGE_ONLY_NO_MODEL_TRAINING",
    }
    (args.output_dir/"report.json").write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False)+"\n", encoding="utf-8")
    _write_markdown(args.output_dir, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-predictions", type=Path, default=DEFAULT_SOURCE)
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

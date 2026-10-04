#!/usr/bin/env python3
"""Canonical table comparator for position-addition golden masters."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def read_table(path):
    path = Path(path)
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        return pd.DataFrame(rows)
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return pd.DataFrame(payload if isinstance(payload, list) else [payload])
    return pd.read_csv(path)


def compare_frames(baseline, candidate, keys=(), ignore=(),
                   float_abs_tolerance=1e-8):
    baseline = baseline.drop(columns=list(ignore), errors="ignore").copy()
    candidate = candidate.drop(columns=list(ignore), errors="ignore").copy()
    common = sorted(set(baseline.columns) & set(candidate.columns))
    baseline = baseline[common]
    candidate = candidate[common]
    sort_keys = [key for key in keys if key in common]
    if sort_keys:
        baseline = baseline.sort_values(sort_keys, kind="mergesort")
        candidate = candidate.sort_values(sort_keys, kind="mergesort")
    baseline = baseline.reset_index(drop=True)
    candidate = candidate.reset_index(drop=True)
    differences = []
    if list(baseline.columns) != list(candidate.columns):
        differences.append({"kind": "columns"})
    if len(baseline) != len(candidate):
        differences.append({
            "kind": "row_count", "baseline": len(baseline),
            "candidate": len(candidate),
        })
    for column in common:
        if len(baseline) != len(candidate):
            break
        left = baseline[column]
        right = candidate[column]
        if pd.api.types.is_numeric_dtype(left) and pd.api.types.is_numeric_dtype(right):
            equal = np.isclose(
                left.to_numpy(dtype=float), right.to_numpy(dtype=float),
                rtol=0.0, atol=float_abs_tolerance, equal_nan=True,
            )
        else:
            def nested_equal(first, second):
                if isinstance(first, dict) and isinstance(second, dict):
                    return (set(first) == set(second) and all(
                        nested_equal(first[key], second[key]) for key in first))
                if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
                    return (len(first) == len(second) and all(
                        nested_equal(a, b) for a, b in zip(first, second)))
                if isinstance(first, (int, float, np.number)) and isinstance(
                        second, (int, float, np.number)):
                    return bool(np.isclose(first, second, rtol=0.0,
                                           atol=float_abs_tolerance,
                                           equal_nan=True))
                try:
                    if pd.isna(first) and pd.isna(second):
                        return True
                except (TypeError, ValueError):
                    pass
                return first == second
            equal = np.asarray([nested_equal(a, b) for a, b in zip(left, right)],
                               dtype=bool)
        failed = np.flatnonzero(~equal)
        if len(failed):
            row = int(failed[0])
            differences.append({
                "kind": "value", "column": column, "row": row,
                "baseline": str(left.iloc[row]),
                "candidate": str(right.iloc[row]),
            })
    return {
        "equal": not differences,
        "baseline_rows": len(baseline),
        "candidate_rows": len(candidate),
        "compared_columns": common,
        "differences": differences,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--float-abs-tolerance", type=float, default=1e-8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare_frames(
        read_table(args.baseline), read_table(args.candidate),
        args.key, args.ignore, args.float_abs_tolerance,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    raise SystemExit(0 if result["equal"] else 1)


if __name__ == "__main__":
    main()

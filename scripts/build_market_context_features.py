#!/usr/bin/env python3
"""Build price-independent PIT market breadth features."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuMarketBreadth import (  # noqa: E402
    MarketBreadthBuilder, load_market_industry_context_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402


def _write_csv(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, compression="gzip")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/shortline_features/m0a"))
    parser.add_argument("--config", type=Path,
                        default=ROOT / "configs/selection/market_industry_context_v1.json")
    parser.add_argument("--start-date", type=int, default=20200101)
    parser.add_argument("--end-date", type=int, default=20261002)
    args = parser.parse_args()

    config = load_market_industry_context_config(args.config)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=args.start_date, end_date=args.end_date)
    frame = MarketBreadthBuilder(panel, config).build(
        args.start_date, args.end_date)
    output = _write_csv(frame, args.output_dir / "market_breadth_asof_close.csv.gz")
    manifest = {
        "milestone": "M0A",
        "dataset": "market_breadth_asof_close",
        "feature_version": config.feature_version,
        "config_sha256": config.sha256,
        "start_date": int(frame.trade_date.min()) if len(frame) else None,
        "end_date": int(frame.trade_date.max()) if len(frame) else None,
        "rows": int(len(frame)),
        "universe_scopes": sorted(frame.universe_scope.unique().tolist()) if len(frame) else [],
        "output": str(output),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "market_breadth_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()

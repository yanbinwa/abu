#!/usr/bin/env python3
"""Generate one browser for every closed trade in the two research strategies."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuTradeVisualization import (  # noqa: E402
    generate_trade_report, write_html_index,
)


DEFAULT_VCP = Path(
    "/Users/wjy/abu/backtests/vcp_optimization_v2_20261003/"
    "h_residual_stop_trailing_stagnation_continuous")
DEFAULT_ALPHA158 = Path(
    "/Users/wjy/abu/backtests/alpha158_lite_low_turnover_v3/"
    "alpha158_lite_low_turnover_v3")


def _chart_path(prefix, row):
    return "{}/charts/{:04d}_{}_{}_{}.png".format(
        prefix, int(row.trade_no), row.symbol,
        int(row.buy_date), int(row.sell_date))


def _open_buys(backtest_dir):
    fills = pd.read_csv(Path(backtest_dir) / "fills.csv")
    fills = fills[fills.status.eq("filled")].reset_index(drop=True)
    active = {}
    for row in fills.itertuples(index=False):
        if row.side == "buy":
            active[str(row.symbol)] = row._asdict()
        elif row.side == "sell":
            active.pop(str(row.symbol), None)
    columns = [
        "symbol", "date", "quantity", "fill_price_raw", "intent_id",
        "actual_initial_r_cash",
    ]
    return pd.DataFrame(active.values()).reindex(columns=columns)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vcp-dir", type=Path, default=DEFAULT_VCP)
    parser.add_argument("--alpha158-dir", type=Path, default=DEFAULT_ALPHA158)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(
            "/Users/wjy/abu/backtests/all_strategy_trade_visualization_20261004"))
    parser.add_argument("--adjusted-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/raw"))
    parser.add_argument("--security-master", type=Path,
                        default=Path(
                            "/Users/wjy/abu/data/selection_research/"
                            "security_master.csv"))
    parser.add_argument("--before", type=int, default=30)
    parser.add_argument("--after", type=int, default=10)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    configurations = [
        ("vcp", args.vcp_dir, "VCP＋残差动量", True),
        ("alpha158", args.alpha158_dir, "Alpha158低频v3", False),
    ]
    reports = []
    counts = {}
    for prefix, source, title, require_closed in configurations:
        output = args.output_dir / prefix
        trades, _ = generate_trade_report(
            source, args.adjusted_dir, args.raw_dir, output,
            security_master=args.security_master,
            before=args.before, after=args.after,
            title="{}逐笔交易 K 线复盘".format(title),
            require_all_closed=require_closed,
        )
        trades = trades.copy()
        trades["strategy_name"] = title
        trades["strategy_trade_no"] = trades.trade_no.astype(int)
        trades["chart_path"] = [
            _chart_path(prefix, row) for row in trades.itertuples(index=False)]
        reports.append(trades)
        counts[prefix] = int(len(trades))

    combined = pd.concat(reports, ignore_index=True)
    combined = combined.sort_values(
        ["buy_date", "strategy_name", "strategy_trade_no"],
        kind="mergesort").reset_index(drop=True)
    combined["trade_no"] = range(1, len(combined)+1)
    combined.to_csv(args.output_dir / "trades_visualized.csv", index=False)
    index = write_html_index(
        combined, args.output_dir, "全部候选策略逐笔交易 K 线复盘")

    open_positions = _open_buys(args.alpha158_dir)
    open_positions.to_csv(args.output_dir / "open_positions.csv", index=False)
    manifest = {
        "closed_trades": int(len(combined)),
        "vcp_closed_trades": counts["vcp"],
        "alpha158_closed_trades": counts["alpha158"],
        "alpha158_open_positions": int(len(open_positions)),
        "index": str(index),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

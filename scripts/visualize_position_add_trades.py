#!/usr/bin/env python3
"""Render OPEN/ADD/REDUCE/CLOSE markers for position-add ledger trades."""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuTradeVisualization import build_position_add_markers


STYLES = {
    "OPEN": ("^", "#159447", "基础买入"),
    "INCREASE": ("^", "#1f77b4", "加仓"),
    "REDUCE": ("v", "#ff7f0e", "部分退出"),
    "CLOSE": ("v", "#d62728", "完全退出"),
}


def _prices(directory, symbol):
    matches = sorted(Path(directory).glob(symbol+"_*"))
    if not matches:
        raise FileNotFoundError(symbol)
    frame = pd.read_csv(matches[-1])
    frame["date"] = pd.to_numeric(
        frame.date.astype(str).str.replace("-", ""), errors="coerce")
    return frame.dropna(subset=["date", "open", "high", "low", "close"])


def render_trade(markers, adjusted_dir, output):
    symbol = str(markers.symbol.iloc[0])
    prices = _prices(adjusted_dir, symbol)
    start, end = int(markers.date.min()), int(markers.date.max())
    dates = prices.date.astype(int).tolist()
    lo = max(0, next(i for i, value in enumerate(dates) if value >= start)-30)
    hi = min(len(prices), max(i for i, value in enumerate(dates) if value <= end)+11)
    frame = prices.iloc[lo:hi].reset_index(drop=True)
    factor = {}
    for row in markers.itertuples(index=False):
        current = frame[frame.date.astype(int).eq(int(row.date))]
        if current.empty:
            continue
        # The marker is placed on adjusted close while its raw execution value
        # stays in the annotation; this avoids silently mixing price spaces.
        factor[int(row.date)] = float(current.iloc[0].close)
    fig, ax = plt.subplots(figsize=(14, 7))
    ax.plot(range(len(frame)), frame.close, color="#333", linewidth=1.1)
    date_x = {int(value): index for index, value in enumerate(frame.date)}
    for row in markers.itertuples(index=False):
        if int(row.date) not in date_x:
            continue
        x = date_x[int(row.date)]
        y = factor[int(row.date)]
        shape, color, label = STYLES.get(row.marker_type, ("o", "#777", row.marker_type))
        ax.scatter([x], [y], marker=shape, color=color, s=90, zorder=5)
        ax.annotate("{} {}股\n{}".format(label, row.quantity, row.reason),
                    (x, y), xytext=(4, 12 if shape == "^" else -30),
                    textcoords="offset points", fontsize=8, color=color)
    ticks = list(range(0, len(frame), max(1, len(frame)//10)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(int(frame.date.iloc[i])) for i in ticks], rotation=35)
    ax.set_title("{} / {}".format(symbol, markers.trade_id.iloc[0]))
    ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(output, dpi=130)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--adjusted-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    charts = args.output_dir/"charts"; charts.mkdir(exist_ok=True)
    fills = pd.read_csv(args.backtest_dir/"physical_fills.csv")
    allocations = pd.read_csv(args.backtest_dir/"fill_allocations.csv")
    dispositions = pd.read_csv(args.backtest_dir/"lot_dispositions.csv")
    markers = build_position_add_markers(fills, allocations, dispositions)
    records = []
    for number, (trade_id, group) in enumerate(
            markers.groupby("trade_id", sort=True), 1):
        path = charts/("{:04d}_{}.png".format(number, trade_id))
        render_trade(group, args.adjusted_dir, path)
        records.append({"trade_no": number, "trade_id": trade_id,
                        "symbol": group.symbol.iloc[0],
                        "markers": len(group), "chart": "charts/"+path.name})
    pd.DataFrame(records).to_csv(args.output_dir/"trades_visualized.csv", index=False)
    cards = "\n".join(
        '<article data-symbol="{}"><h3>#{} {} {}</h3><p>{} 个账本标记</p>'
        '<img loading="lazy" src="{}"></article>'.format(
            html.escape(row["symbol"]), row["trade_no"],
            html.escape(row["symbol"]), html.escape(row["trade_id"]),
            row["markers"], html.escape(row["chart"])) for row in records)
    page = """<!doctype html><meta charset='utf-8'><title>加仓交易复盘</title>
<style>body{{font-family:sans-serif;background:#f5f5f5;margin:20px}}article{{background:white;padding:14px;margin:14px 0;border-radius:8px}}img{{width:100%}}</style>
<h1>加仓策略逐笔 K 线复盘</h1><input id='q' placeholder='股票代码'>
<div id='cards'>{}</div><script>q.oninput=()=>document.querySelectorAll('article').forEach(x=>x.hidden=!x.dataset.symbol.includes(q.value))</script>""".format(cards)
    (args.output_dir/"index.html").write_text(page, encoding="utf-8")
    (args.output_dir/"manifest.json").write_text(json.dumps({
        "trades": len(records), "markers": len(markers),
        "index": str(args.output_dir/"index.html")}, ensure_ascii=False,
        indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()

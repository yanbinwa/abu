#!/usr/bin/env python3
"""Queue de-duplicated WeCom notices for newly filled paper trades."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.queue_wecom_strategy import queue_message  # noqa: E402


EXIT_REASON_LABELS = {
    "INITIAL_STOP": "初始止损",
    "TRAILING_STOP": "移动止损",
    "STAGNATION": "停滞退出",
    "BREAKOUT_FAILURE": "突破失败",
    "MARKET_REGIME": "市场状态退出",
    "FIXED_HOLD": "固定持有期退出",
}


def _atomic_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def pending_fills(paper_dir):
    paper_dir = Path(paper_dir)
    fills_path = paper_dir / "fills.csv"
    if not fills_path.exists() or fills_path.stat().st_size == 0:
        return pd.DataFrame(), {"notified_order_ids": []}
    try:
        fills = pd.read_csv(fills_path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), {"notified_order_ids": []}
    if fills.empty:
        return fills, {"notified_order_ids": []}
    fills = fills[
        fills.status.astype(str).eq("filled") &
        fills.side.astype(str).isin(("buy", "sell"))].copy()
    state_path = paper_dir / "wecom_notification_state.json"
    state = (json.loads(state_path.read_text(encoding="utf-8"))
             if state_path.exists() else {"notified_order_ids": []})
    notified = set(state.get("notified_order_ids", []))
    return fills[~fills.order_id.astype(str).isin(notified)].copy(), state


def build_trade_message(fills, summary, names=None, exit_reasons=None):
    names = names or {}
    exit_reasons = (exit_reasons if exit_reasons is not None else
                    pd.DataFrame(columns=["date", "symbol", "reason"]))
    latest_date = int(pd.to_numeric(fills.date).max())
    lines = ["## VCP 模拟盘成交提醒", "", "行情日：{}".format(latest_date)]
    for row in fills.sort_values(["date", "side", "symbol"]).itertuples(index=False):
        symbol = str(row.symbol)
        name = str(names.get(symbol, ""))
        if row.side == "buy":
            direction, reason = "买入", "VCP 收缩突破 + 正残差动量，风险审批通过"
        else:
            direction = "卖出"
            candidates = exit_reasons[
                exit_reasons.symbol.astype(str).eq(symbol) &
                (pd.to_numeric(exit_reasons.date) <= int(row.date))]
            code = (str(candidates.sort_values("date").iloc[-1].reason)
                    if len(candidates) else "UNKNOWN")
            reason = EXIT_REASON_LABELS.get(code, code)
        fees = sum(float(getattr(row, field, 0.0) or 0.0)
                   for field in ("commission", "transfer_fee", "stamp_tax"))
        lines.extend([
            "", "**{} {} {}**".format(direction, symbol, name).strip(),
            "- 成交价：¥{:.3f}".format(float(row.fill_price_raw)),
            "- 数量：{:,} 股".format(int(row.quantity)),
            "- 原因：{}".format(reason),
            "- 费用 / 滑点：¥{:.2f} / ¥{:.2f}".format(
                fees, float(getattr(row, "slippage_cost", 0.0) or 0.0)),
        ])
    lines.extend([
        "", "账户资产：¥{:,.2f}".format(float(summary["capital"])),
        "累计收益：{:+.3f}%".format(float(summary["return_pct"])),
        "当前仓位：{:.2f}%（{} 只）".format(
            float(summary["exposure_pct"]), int(summary["positions"])),
        "", "研究模拟盘，不是实盘委托。",
    ])
    return "\n".join(lines)


def notify(paper_dir, security_master, dry_run=False):
    paper_dir = Path(paper_dir)
    fills, notification_state = pending_fills(paper_dir)
    if fills.empty:
        return {"status": "no_new_fills", "queued": 0}
    summary = json.loads((paper_dir / "summary.json").read_text(encoding="utf-8"))
    master = pd.read_csv(security_master, dtype={"symbol": str})
    names = dict(zip(master.symbol, master.name))
    exit_path = paper_dir / "exit_reasons.csv"
    try:
        exits = pd.read_csv(exit_path) if exit_path.exists() else pd.DataFrame()
    except pd.errors.EmptyDataError:
        exits = pd.DataFrame()
    if exits.empty:
        exits = pd.DataFrame(columns=["date", "symbol", "reason"])
    content = build_trade_message(fills, summary, names, exits)
    (paper_dir / "last_wecom_notification.md").write_text(
        content + "\n", encoding="utf-8")
    if dry_run:
        return {"status": "dry_run", "queued": 0, "fills": len(fills),
                "content": content}
    queue_id = queue_message(content)
    notified = list(notification_state.get("notified_order_ids", []))
    notified.extend(fills.order_id.astype(str).tolist())
    notification_state = {
        "notified_order_ids": list(dict.fromkeys(notified)),
        "last_queue_id": queue_id,
        "last_market_date": int(pd.to_numeric(fills.date).max()),
    }
    _atomic_json(paper_dir / "wecom_notification_state.json", notification_state)
    return {"status": "queued", "queued": 1, "fills": len(fills),
            "queue_id": queue_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-dir", type=Path,
                        default=Path("/Users/wjy/abu/paper/vcp_residual_v2"))
    parser.add_argument("--security-master", type=Path,
                        default=Path(
                            "/Users/wjy/abu/data/selection_research/security_master.csv"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = notify(args.paper_dir, args.security_master, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

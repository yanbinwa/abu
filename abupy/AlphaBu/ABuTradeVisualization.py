# -*- encoding: utf-8 -*-
"""Render audited VCP round trips on adjusted candlestick charts."""
from __future__ import annotations

import ast
import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd


EXIT_REASON_LABELS = {
    "INITIAL_STOP": "初始止损",
    "TRAILING_STOP": "移动止损",
    "STAGNATION": "停滞退出",
    "BREAKOUT_FAILURE": "突破失败",
    "MARKET_REGIME": "市场状态退出",
    "FIXED_HOLD": "固定持有期退出",
}

EXIT_REASON_DETAILS = {
    "INITIAL_STOP": "收盘价触及初始止损，下一可交易日开盘卖出",
    "TRAILING_STOP": "浮盈达到 1R 后，收盘价触及峰值减 3ATR 的移动止损",
    "STAGNATION": "持有至少 20 个交易日，最大有利变动仍不足 0.5R",
    "BREAKOUT_FAILURE": "突破后五日内收盘价重新跌回突破位",
    "MARKET_REGIME": "基准指数收盘价跌破 MA200",
    "FIXED_HOLD": "达到固定持有期限",
}


def _metadata(value):
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return {}
    try:
        parsed = ast.literal_eval(str(value))
    except (SyntaxError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fees(row):
    return sum(float(row.get(name, 0.0) or 0.0)
               for name in ("commission", "transfer_fee", "stamp_tax"))


def build_round_trips(fills, intents, exit_reasons, position_events=None):
    """Pair filled buys and sells and independently calculate trade results."""
    fills = fills.copy()
    fills["_sequence"] = np.arange(len(fills))
    fills = fills[fills.status.eq("filled")].sort_values(["date", "_sequence"])
    intent_map = {str(row.intent_id): row for row in intents.itertuples(index=False)}
    exits = exit_reasons.copy()
    events = (position_events.copy() if position_events is not None
              else pd.DataFrame(columns=["date", "symbol", "event_type",
                                         "cash_delta", "reason"]))
    active = {}
    records = []
    for row in fills.to_dict("records"):
        symbol = str(row["symbol"])
        if row["side"] == "buy":
            if symbol in active:
                raise ValueError("overlapping buy fills for {}".format(symbol))
            active[symbol] = row
            continue
        if row["side"] != "sell":
            continue
        if symbol not in active:
            raise ValueError("sell fill without a paired buy for {}".format(symbol))
        buy = active.pop(symbol)
        intent = intent_map.get(str(buy["intent_id"]))
        metadata = _metadata(getattr(intent, "metadata", None))
        candidates = exits[
            exits.symbol.astype(str).eq(symbol) &
            (pd.to_numeric(exits.date) >= int(buy["date"])) &
            (pd.to_numeric(exits.date) <= int(row["date"]))
        ]
        exit_row = candidates.sort_values("date").iloc[-1] if len(candidates) else None
        reason = str(exit_row.reason) if exit_row is not None else "UNKNOWN"
        signal_date = int(getattr(intent, "signal_asof", buy["date"]))
        exit_signal_date = int(exit_row.date) if exit_row is not None else int(row["date"])
        trade_events = events[
            events.symbol.astype(str).eq(symbol) &
            (pd.to_numeric(events.date) >= int(buy["date"])) &
            (pd.to_numeric(events.date) <= int(row["date"]))
        ]
        cash_dividend = float(pd.to_numeric(
            trade_events.get("cash_delta", pd.Series(dtype=float)),
            errors="coerce").fillna(0).sum())
        buy_value = int(buy["quantity"]) * float(buy["fill_price_raw"])
        sell_value = int(row["quantity"]) * float(row["fill_price_raw"])
        buy_fees, sell_fees = _fees(buy), _fees(row)
        net_pnl = sell_value - sell_fees + cash_dividend - buy_value - buy_fees
        initial_r_cash = float(buy.get("actual_initial_r_cash", 0.0) or 0.0)
        records.append({
            "trade_no": len(records) + 1,
            "symbol": symbol,
            "strategy_id": str(getattr(intent, "strategy_id", "")),
            "signal_date": signal_date,
            "buy_date": int(buy["date"]),
            "buy_quantity": int(buy["quantity"]),
            "buy_price_raw": float(buy["fill_price_raw"]),
            "sell_signal_date": exit_signal_date,
            "sell_date": int(row["date"]),
            "sell_quantity": int(row["quantity"]),
            "sell_price_raw": float(row["fill_price_raw"]),
            "score": float(getattr(intent, "score", np.nan)),
            "residual_momentum": float(metadata.get("residual_momentum", np.nan)),
            "breakout_level_adjusted": float(metadata.get("breakout_level", np.nan)),
            "adjustment_factor_signal": float(
                getattr(intent, "adjustment_factor_signal", np.nan)),
            "initial_stop_raw": float(getattr(intent, "initial_stop_raw", np.nan)),
            "max_buy_price_raw": float(metadata.get("max_buy_price_raw", np.nan)),
            "exit_reason": reason,
            "exit_reason_cn": EXIT_REASON_LABELS.get(reason, reason),
            "cash_dividend": cash_dividend,
            "buy_fees": buy_fees,
            "sell_fees": sell_fees,
            "slippage_cost": float(buy.get("slippage_cost", 0.0) or 0.0) +
                             float(row.get("slippage_cost", 0.0) or 0.0),
            "net_pnl": net_pnl,
            "return_pct": net_pnl / (buy_value + buy_fees) * 100,
            "r_multiple": (net_pnl / initial_r_cash
                           if initial_r_cash > 0 else np.nan),
            "event_count": len(trade_events),
        })
    if active:
        raise ValueError("unclosed filled buys: {}".format(sorted(active)))
    return pd.DataFrame(records)


def _configure_chinese_font():
    candidates = ("PingFang SC", "Hiragino Sans GB", "Arial Unicode MS",
                  "Noto Sans CJK SC", "Microsoft YaHei")
    installed = {item.name for item in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False


def _load_price_file(directory, symbol, adjusted):
    directory = Path(directory)
    if adjusted:
        matches = sorted(directory.glob(symbol + "_*"))
        if not matches:
            raise FileNotFoundError("adjusted price file missing for {}".format(symbol))
        frame = pd.read_csv(matches[-1])
        date_column = "date" if "date" in frame else "date_time"
    else:
        path = directory / (symbol + ".csv")
        if not path.exists():
            raise FileNotFoundError("raw price file missing: {}".format(path))
        frame = pd.read_csv(path)
        date_column = "date"
    frame["date"] = pd.to_numeric(frame[date_column].astype(str).str.replace("-", ""),
                                   errors="coerce").astype("Int64")
    for field in ("open", "high", "low", "close", "volume"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    return frame.dropna(subset=["date", "open", "high", "low", "close"]).copy()


def _window(frame, start_date, end_date, before, after):
    dates = frame.date.astype(int).to_numpy()
    start = max(0, int(np.searchsorted(dates, start_date, side="left")) - before)
    end = min(len(frame), int(np.searchsorted(dates, end_date, side="right")) + after)
    return frame.iloc[start:end].reset_index(drop=True)


def _factor_on(raw, adjusted, date):
    raw_row = raw[raw.date.astype(int).eq(int(date))]
    adj_row = adjusted[adjusted.date.astype(int).eq(int(date))]
    if raw_row.empty or adj_row.empty or float(adj_row.iloc[0].close) <= 0:
        return np.nan
    return float(raw_row.iloc[0].close) / float(adj_row.iloc[0].close)


def render_trade_chart(trade, adjusted_prices, raw_prices, output_path,
                       stock_name="", position_events=None, before=30, after=10):
    """Render one round trip; the K-line uses qfq prices used by the signal."""
    _configure_chinese_font()
    frame = _window(adjusted_prices, int(trade.signal_date), int(trade.sell_date),
                    before, after)
    if frame.empty:
        raise ValueError("empty chart window for {}".format(trade.symbol))
    frame["datetime"] = pd.to_datetime(frame.date.astype(str), format="%Y%m%d")
    x = np.arange(len(frame), dtype=float)
    date_to_x = {int(date): i for i, date in enumerate(frame.date)}
    buy_factor = _factor_on(raw_prices, adjusted_prices, int(trade.buy_date))
    sell_factor = _factor_on(raw_prices, adjusted_prices, int(trade.sell_date))
    buy_y = float(trade.buy_price_raw) / buy_factor
    sell_y = float(trade.sell_price_raw) / sell_factor
    signal_factor = float(trade.adjustment_factor_signal)
    stop_y = float(trade.initial_stop_raw) / signal_factor

    fig = plt.figure(figsize=(15, 9), constrained_layout=True)
    grid = fig.add_gridspec(3, 1, height_ratios=[5.2, 1.3, 1.25])
    ax = fig.add_subplot(grid[0])
    volume_ax = fig.add_subplot(grid[1], sharex=ax)
    note_ax = fig.add_subplot(grid[2])
    note_ax.axis("off")

    width = 0.62
    colors = []
    for i, row in frame.iterrows():
        up = row.close >= row.open
        color = "#d62728" if up else "#159447"
        colors.append(color)
        ax.vlines(i, row.low, row.high, color=color, linewidth=1)
        bottom = min(row.open, row.close)
        height = max(abs(row.close - row.open), max(row.close, 1.0) * 0.0005)
        ax.add_patch(Rectangle((i-width/2, bottom), width, height,
                               facecolor=color, edgecolor=color, linewidth=0.7))
    close = frame.close.astype(float)
    ax.plot(x, close.rolling(20).mean(), color="#ff8c00", linewidth=1.2,
            label="MA20")
    ax.plot(x, close.rolling(60, min_periods=20).mean(), color="#4169e1",
            linewidth=1.1, label="MA60")
    buy_x, sell_x = date_to_x[int(trade.buy_date)], date_to_x[int(trade.sell_date)]
    signal_x = date_to_x.get(int(trade.signal_date), buy_x)
    exit_signal_x = date_to_x.get(int(trade.sell_signal_date), sell_x)
    ax.axvspan(buy_x, sell_x, color="#5b8ff9", alpha=0.08, label="持有区间")
    ax.axvline(signal_x, color="#8a2be2", linestyle=":", linewidth=1)
    ax.axvline(exit_signal_x, color="#444444", linestyle=":", linewidth=1)
    ax.hlines(stop_y, signal_x, sell_x, color="#dc143c", linestyle="--",
              linewidth=1.1, label="初始止损")
    breakout = float(trade.breakout_level_adjusted)
    if np.isfinite(breakout):
        ax.hlines(breakout, max(0, signal_x-20), buy_x, color="#8a2be2",
                  linestyle="--", linewidth=1.0, label="突破位")
    ax.scatter([buy_x], [buy_y], marker="^", s=150, color="#d62728",
               edgecolor="black", linewidth=0.6, zorder=6)
    ax.scatter([sell_x], [sell_y], marker="v", s=150, color="#159447",
               edgecolor="black", linewidth=0.6, zorder=6)
    ax.annotate("买入\n¥{:.3f}".format(float(trade.buy_price_raw)),
                (buy_x, buy_y), xytext=(0, -46), textcoords="offset points",
                ha="center", arrowprops={"arrowstyle": "->", "color": "#d62728"},
                fontsize=9)
    ax.annotate("卖出：{}\n¥{:.3f}\n{:+.2f}% / {:+.2f}R".format(
                    trade.exit_reason_cn, float(trade.sell_price_raw),
                    float(trade.return_pct), float(trade.r_multiple)),
                (sell_x, sell_y), xytext=(0, 42), textcoords="offset points",
                ha="center", arrowprops={"arrowstyle": "->", "color": "#159447"},
                fontsize=9)

    event_rows = (position_events if position_events is not None
                  else pd.DataFrame())
    if not event_rows.empty:
        relevant = event_rows[
            event_rows.symbol.astype(str).eq(str(trade.symbol)) &
            (pd.to_numeric(event_rows.date) >= int(trade.buy_date)) &
            (pd.to_numeric(event_rows.date) <= int(trade.sell_date))]
        for event in relevant.itertuples(index=False):
            event_x = date_to_x.get(int(event.date))
            if event_x is not None:
                ax.axvline(event_x, color="#daa520", linestyle="-.", linewidth=1)
                ax.text(event_x, ax.get_ylim()[1], "公司行为", color="#8b6508",
                        ha="center", va="bottom", fontsize=8)

    title_name = "{} {}".format(trade.symbol, stock_name).strip()
    ax.set_title("第 {} 笔｜{}｜{} → {}｜净盈亏 ¥{:,.0f}".format(
        int(trade.trade_no), title_name, int(trade.buy_date), int(trade.sell_date),
        float(trade.net_pnl)), fontsize=14)
    ax.set_ylabel("前复权价格")
    ax.grid(axis="y", alpha=0.18)
    ax.legend(loc="upper left", ncol=5, fontsize=8)

    volumes = frame.volume.fillna(0).astype(float).to_numpy()
    volume_ax.bar(x, volumes, width=width, color=colors, alpha=0.55)
    volume_ax.set_ylabel("成交量")
    volume_ax.grid(axis="y", alpha=0.14)
    tick_step = max(1, len(frame) // 10)
    ticks = np.arange(0, len(frame), tick_step)
    volume_ax.set_xticks(ticks)
    volume_ax.set_xticklabels(
        [frame.iloc[int(i)].datetime.strftime("%Y-%m-%d") for i in ticks],
        rotation=30, ha="right", fontsize=8)
    plt.setp(ax.get_xticklabels(), visible=False)

    residual = ("{:+.2%}".format(float(trade.residual_momentum))
                if np.isfinite(float(trade.residual_momentum)) else "无")
    buy_reason = ("VCP 收缩后突破；趋势过滤通过；中期残差动量为正；"
                  "组合风险审批通过")
    detail = EXIT_REASON_DETAILS.get(str(trade.exit_reason), str(trade.exit_reason))
    lines = [
        "买入信号：{}（信号日 {}，次日开盘成交）".format(
            buy_reason, int(trade.signal_date)),
        "入场依据：综合评分 {:.3f}；残差动量 {}；初始止损 ¥{:.3f}；最高允许买价 ¥{:.3f}".format(
            float(trade.score), residual, float(trade.initial_stop_raw),
            float(trade.max_buy_price_raw)),
        "卖出信号：{}（{}）；信号日 {}，实际成交日 {}".format(
            trade.exit_reason_cn, detail, int(trade.sell_signal_date),
            int(trade.sell_date)),
        "结果：数量 {:,}→{:,} 股；净盈亏 ¥{:,.2f}；收益 {:+.2f}%；{:+.2f}R；费用 ¥{:,.2f}；滑点 ¥{:,.2f}；现金分红 ¥{:,.2f}".format(
            int(trade.buy_quantity), int(trade.sell_quantity), float(trade.net_pnl),
            float(trade.return_pct), float(trade.r_multiple),
            float(trade.buy_fees + trade.sell_fees), float(trade.slippage_cost),
            float(trade.cash_dividend)),
    ]
    note_ax.text(0.01, 0.94, "\n".join(lines), ha="left", va="top",
                 fontsize=10, linespacing=1.45,
                 bbox={"facecolor": "#f6f8fa", "edgecolor": "#d0d7de",
                       "boxstyle": "round,pad=0.55"})
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=135, bbox_inches="tight")
    plt.close(fig)
    return output_path


def write_html_index(trades, output_dir, title):
    output_dir = Path(output_dir)
    wins = int((trades.net_pnl > 0).sum())
    payload = []
    for trade in trades.itertuples(index=False):
        image_name = "charts/{:04d}_{}_{}_{}.png".format(
            int(trade.trade_no), trade.symbol, int(trade.buy_date), int(trade.sell_date))
        def number(value):
            value = float(value)
            return value if np.isfinite(value) else None

        payload.append({
            "tradeNo": int(trade.trade_no), "symbol": str(trade.symbol),
            "stockName": str(getattr(trade, "stock_name", "")),
            "signalDate": int(trade.signal_date), "buyDate": int(trade.buy_date),
            "sellSignalDate": int(trade.sell_signal_date),
            "sellDate": int(trade.sell_date),
            "buyQuantity": int(trade.buy_quantity),
            "sellQuantity": int(trade.sell_quantity),
            "buyPrice": number(trade.buy_price_raw),
            "sellPrice": number(trade.sell_price_raw),
            "score": number(trade.score),
            "residualMomentum": number(trade.residual_momentum),
            "initialStop": number(trade.initial_stop_raw),
            "maxBuyPrice": number(trade.max_buy_price_raw),
            "exitReason": str(trade.exit_reason),
            "exitReasonCn": str(trade.exit_reason_cn),
            "exitDetail": EXIT_REASON_DETAILS.get(
                str(trade.exit_reason), str(trade.exit_reason)),
            "cashDividend": number(trade.cash_dividend),
            "fees": number(trade.buy_fees + trade.sell_fees),
            "slippage": number(trade.slippage_cost),
            "netPnl": number(trade.net_pnl),
            "returnPct": number(trade.return_pct),
            "rMultiple": number(trade.r_multiple),
            "image": image_name,
        })
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data_json = data_json.replace("</", "<\\/")
    content = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f7fb;--panel:#fff;--ink:#172033;--muted:#667085;--line:#e5e9f0;--blue:#315efb;--blue-soft:#eef3ff;--win:#d92d20;--loss:#07845a;--shadow:0 12px 34px rgba(35,47,75,.08)}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
button,input,select{font:inherit}.topbar{padding:24px 28px 18px;background:linear-gradient(135deg,#101828,#22376f);color:#fff}
.topbar h1{margin:0 0 6px;font-size:24px}.topbar p{margin:0;color:#cbd5e1;font-size:13px}.stats{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:12px;margin:18px 28px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}.stat small{color:var(--muted)}.stat strong{display:block;margin-top:5px;font-size:20px}
.workspace{display:grid;grid-template-columns:340px minmax(0,1fr);gap:16px;padding:0 28px 28px;min-height:calc(100vh - 190px)}
.sidebar,.viewer{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}.filters{padding:16px;border-bottom:1px solid var(--line)}
.filters input,.filters select,.jump select{width:100%;border:1px solid #d0d5dd;border-radius:8px;padding:9px 10px;background:#fff;color:var(--ink)}.filter-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.list-head{display:flex;justify-content:space-between;padding:11px 16px;color:var(--muted);font-size:12px}.trade-list{height:calc(100vh - 365px);min-height:420px;overflow:auto;padding:0 8px 10px}
.trade-item{width:100%;border:0;border-radius:9px;background:transparent;padding:10px;text-align:left;cursor:pointer;display:grid;grid-template-columns:42px 1fr auto;gap:7px;align-items:center;color:var(--ink)}
.trade-item:hover{background:#f6f8fc}.trade-item.active{background:var(--blue-soft);outline:1px solid #c7d7fe}.trade-no{color:var(--muted);font-size:12px}.trade-main{min-width:0}.trade-symbol{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.trade-meta{font-size:11px;color:var(--muted);margin-top:3px}.pnl{text-align:right;font-weight:700}.win{color:var(--win)}.loss{color:var(--loss)}
.viewer{display:flex;flex-direction:column}.viewer-bar{display:flex;gap:10px;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line)}.jump{min-width:280px;flex:1}.nav-button,.open-button{border:1px solid #d0d5dd;background:#fff;border-radius:8px;padding:9px 13px;cursor:pointer;color:var(--ink)}.nav-button:hover,.open-button:hover{border-color:var(--blue);color:var(--blue)}.nav-button:disabled{opacity:.4;cursor:not-allowed}
.chart-wrap{background:#fafbfc;position:relative;min-height:430px;display:flex;align-items:center;justify-content:center;padding:12px}.chart-wrap img{display:block;max-width:100%;max-height:68vh;object-fit:contain;cursor:zoom-in}.chart-empty{color:var(--muted)}
.detail{border-top:1px solid var(--line);padding:18px}.detail-title{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}.detail-title h2{margin:0;font-size:20px}.badge{border-radius:999px;padding:4px 9px;background:#f2f4f7;color:#344054;font-size:12px}.detail-grid{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin-top:14px}.metric{background:#f8fafc;border-radius:9px;padding:10px 12px}.metric small{display:block;color:var(--muted);margin-bottom:5px}.metric b{font-size:15px}.reason{margin-top:12px;padding:12px 14px;border-left:3px solid var(--blue);background:var(--blue-soft);font-size:13px;line-height:1.7}
.hint{color:var(--muted);font-size:11px;margin-left:auto}@media(max-width:960px){.stats{grid-template-columns:repeat(2,1fr)}.workspace{grid-template-columns:1fr}.trade-list{height:300px;min-height:0}.detail-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:560px){.topbar,.workspace{padding-left:14px;padding-right:14px}.stats{margin-left:14px;margin-right:14px}.viewer-bar{flex-wrap:wrap}.jump{min-width:100%;order:-1}.hint{display:none}}
</style></head><body>
<header class="topbar"><h1>__TITLE__</h1><p>选择任意交易查看完整 K 线、成交位置和策略原因。图表使用前复权价格，标注显示真实未复权成交价。</p></header>
<section class="stats"><div class="stat"><small>完整交易</small><strong>__COUNT__ 笔</strong></div><div class="stat"><small>完整现金流胜率</small><strong>__WIN_RATE__%</strong></div><div class="stat"><small>净盈亏</small><strong class="__PNL_CLASS__">__PNL__</strong></div><div class="stat"><small>平均每笔收益</small><strong>__AVG__%</strong></div></section>
<main class="workspace"><aside class="sidebar"><div class="filters"><input id="search" type="search" placeholder="搜索股票代码或名称" aria-label="搜索交易"><div class="filter-row"><select id="reasonFilter" aria-label="退出原因"><option value="all">全部退出原因</option></select><select id="outcomeFilter" aria-label="盈亏结果"><option value="all">全部结果</option><option value="win">盈利</option><option value="loss">亏损</option></select></div><div class="filter-row"><select id="sortBy" aria-label="排序"><option value="time">按交易时间</option><option value="pnlDesc">盈利从高到低</option><option value="pnlAsc">亏损从低到高</option><option value="rDesc">R 倍数从高到低</option></select><button class="nav-button" id="resetFilter">重置筛选</button></div></div><div class="list-head"><span>交易列表</span><span id="visibleCount"></span></div><div class="trade-list" id="tradeList"></div></aside>
<section class="viewer"><div class="viewer-bar"><button class="nav-button" id="prevTrade">← 上一笔</button><div class="jump"><select id="tradeSelect" aria-label="选择交易"></select></div><button class="nav-button" id="nextTrade">下一笔 →</button><a class="open-button" id="openOriginal" target="_blank" rel="noopener">查看原图</a><span class="hint">键盘 ← → 切换</span></div><div class="chart-wrap"><img id="tradeChart" alt="所选交易 K 线图"><div class="chart-empty" id="emptyState" hidden>没有符合筛选条件的交易</div></div><div class="detail" id="detail"></div></section></main>
<script id="trade-data" type="application/json">__TRADE_DATA__</script>
<script>
const allTrades=JSON.parse(document.getElementById('trade-data').textContent);let visible=[...allTrades],selected=null;
const $=id=>document.getElementById(id);const money=v=>`${v>=0?'+':'-'}¥${Math.abs(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`;const signed=(v,d=2)=>`${v>=0?'+':''}${v.toFixed(d)}`;const pct=v=>`${signed(v)}%`;const date=v=>String(v).replace(/(\d{4})(\d{2})(\d{2})/,'$1-$2-$3');const cls=v=>v>0?'win':'loss';const safe=s=>String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function label(t){return `#${t.tradeNo} · ${t.symbol} ${t.stockName} · ${date(t.buyDate)} · ${t.exitReasonCn} · ${pct(t.returnPct)}`}
function applyFilters(){const q=$('search').value.trim().toLowerCase(),reason=$('reasonFilter').value,outcome=$('outcomeFilter').value,sort=$('sortBy').value;visible=allTrades.filter(t=>(!q||`${t.symbol} ${t.stockName}`.toLowerCase().includes(q))&&(reason==='all'||t.exitReason===reason)&&(outcome==='all'||(outcome==='win'?t.netPnl>0:t.netPnl<=0)));if(sort==='pnlDesc')visible.sort((a,b)=>b.netPnl-a.netPnl);else if(sort==='pnlAsc')visible.sort((a,b)=>a.netPnl-b.netPnl);else if(sort==='rDesc')visible.sort((a,b)=>b.rMultiple-a.rMultiple);else visible.sort((a,b)=>a.tradeNo-b.tradeNo);renderControls();const keep=visible.find(t=>selected&&t.tradeNo===selected.tradeNo);selectTrade(keep||visible[0]||null)}
function renderControls(){$('visibleCount').textContent=`${visible.length} / ${allTrades.length}`;$('tradeList').innerHTML=visible.map(t=>`<button class="trade-item ${selected&&selected.tradeNo===t.tradeNo?'active':''}" data-id="${t.tradeNo}"><span class="trade-no">#${t.tradeNo}</span><span class="trade-main"><span class="trade-symbol">${safe(t.symbol)} ${safe(t.stockName)}</span><span class="trade-meta">${date(t.buyDate)} · ${safe(t.exitReasonCn)}</span></span><span class="pnl ${cls(t.netPnl)}">${pct(t.returnPct)}</span></button>`).join('');$('tradeSelect').innerHTML=visible.map(t=>`<option value="${t.tradeNo}">${safe(label(t))}</option>`).join('');document.querySelectorAll('.trade-item').forEach(el=>el.onclick=()=>selectTrade(allTrades.find(t=>t.tradeNo===Number(el.dataset.id))))}
function selectTrade(t){selected=t;const has=Boolean(t);$('tradeChart').hidden=!has;$('emptyState').hidden=has;$('detail').hidden=!has;$('openOriginal').hidden=!has;if(!has){$('prevTrade').disabled=true;$('nextTrade').disabled=true;return}const index=visible.findIndex(x=>x.tradeNo===t.tradeNo);$('tradeSelect').value=String(t.tradeNo);$('tradeChart').src=t.image;$('tradeChart').alt=`${t.symbol} ${t.stockName} 第 ${t.tradeNo} 笔交易 K 线`;$('openOriginal').href=t.image;$('prevTrade').disabled=index<=0;$('nextTrade').disabled=index<0||index>=visible.length-1;$('detail').innerHTML=`<div class="detail-title"><h2>#${t.tradeNo} ${safe(t.symbol)} ${safe(t.stockName)}</h2><span class="badge">${date(t.buyDate)} → ${date(t.sellDate)}</span><span class="badge">${safe(t.exitReasonCn)}</span></div><div class="detail-grid"><div class="metric"><small>净盈亏</small><b class="${cls(t.netPnl)}">${money(t.netPnl)}</b></div><div class="metric"><small>交易收益</small><b class="${cls(t.returnPct)}">${pct(t.returnPct)}</b></div><div class="metric"><small>R 倍数</small><b>${signed(t.rMultiple)}R</b></div><div class="metric"><small>残差动量</small><b>${signed(t.residualMomentum*100)}%</b></div><div class="metric"><small>买入价 / 数量</small><b>¥${t.buyPrice.toFixed(3)} / ${t.buyQuantity.toLocaleString()}股</b></div><div class="metric"><small>卖出价 / 数量</small><b>¥${t.sellPrice.toFixed(3)} / ${t.sellQuantity.toLocaleString()}股</b></div><div class="metric"><small>费用 / 滑点</small><b>¥${t.fees.toFixed(2)} / ¥${t.slippage.toFixed(2)}</b></div><div class="metric"><small>现金分红</small><b>¥${t.cashDividend.toFixed(2)}</b></div></div><div class="reason"><b>买入：</b>VCP 收缩后突破，趋势过滤和正残差动量条件通过，组合风险审批通过。信号日 ${date(t.signalDate)}，最高允许买价 ¥${t.maxBuyPrice.toFixed(3)}，初始止损 ¥${t.initialStop.toFixed(3)}。<br><b>卖出：</b>${safe(t.exitReasonCn)}——${safe(t.exitDetail)}。信号日 ${date(t.sellSignalDate)}，实际成交日 ${date(t.sellDate)}。</div>`;history.replaceState(null,'',`#trade=${t.tradeNo}`);renderControls();setTimeout(()=>document.querySelector(`.trade-item[data-id="${t.tradeNo}"]`)?.scrollIntoView({block:'nearest'}),0)}
function move(delta){if(!selected)return;const i=visible.findIndex(t=>t.tradeNo===selected.tradeNo),next=visible[i+delta];if(next)selectTrade(next)}
const reasons=[...new Map(allTrades.map(t=>[t.exitReason,t.exitReasonCn])).entries()];$('reasonFilter').insertAdjacentHTML('beforeend',reasons.map(([v,n])=>`<option value="${safe(v)}">${safe(n)}</option>`).join(''));['search','reasonFilter','outcomeFilter','sortBy'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',applyFilters));$('tradeSelect').onchange=e=>selectTrade(allTrades.find(t=>t.tradeNo===Number(e.target.value)));$('prevTrade').onclick=()=>move(-1);$('nextTrade').onclick=()=>move(1);$('resetFilter').onclick=()=>{$('search').value='';$('reasonFilter').value='all';$('outcomeFilter').value='all';$('sortBy').value='time';applyFilters()};$('tradeChart').onclick=()=>selected&&window.open(selected.image,'_blank');document.addEventListener('keydown',e=>{if(e.target.matches('input,select'))return;if(e.key==='ArrowLeft')move(-1);if(e.key==='ArrowRight')move(1)});const requested=Number(location.hash.match(/trade=(\d+)/)?.[1]);selected=allTrades.find(t=>t.tradeNo===requested)||allTrades[0]||null;applyFilters();
</script></body></html>"""
    replacements = {
        "__TITLE__": html.escape(title), "__COUNT__": str(len(trades)),
        "__WIN_RATE__": "{:.2f}".format(
            wins / len(trades) * 100 if len(trades) else 0),
        "__PNL__": "{:+,.2f} 元".format(float(trades.net_pnl.sum())),
        "__PNL_CLASS__": "win" if float(trades.net_pnl.sum()) > 0 else "loss",
        "__AVG__": "{:+.2f}".format(float(trades.return_pct.mean())),
        "__TRADE_DATA__": data_json,
    }
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    path = output_dir / "index.html"
    path.write_text(content, encoding="utf-8")
    return path


def generate_trade_report(backtest_dir, adjusted_dir, raw_dir, output_dir,
                          security_master=None, symbol=None, limit=None,
                          before=30, after=10):
    """Generate the CSV, per-trade charts and a browsable HTML index."""
    backtest_dir, output_dir = Path(backtest_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fills = pd.read_csv(backtest_dir / "fills.csv")
    intents = pd.read_csv(backtest_dir / "intents.csv")
    exits = pd.read_csv(backtest_dir / "exit_reasons.csv")
    event_path = backtest_dir / "position_events.csv"
    events = pd.read_csv(event_path) if event_path.exists() else None
    trades = build_round_trips(fills, intents, exits, events)
    if symbol:
        trades = trades[trades.symbol.eq(symbol)].copy()
    if limit is not None:
        trades = trades.head(int(limit)).copy()
    names = {}
    if security_master and Path(security_master).exists():
        master = pd.read_csv(security_master, dtype={"symbol": str})
        names = dict(zip(master.symbol, master.name))
    trades["stock_name"] = trades.symbol.map(names).fillna("")
    cache = {}
    for trade in trades.itertuples(index=False):
        if trade.symbol not in cache:
            cache[trade.symbol] = (
                _load_price_file(adjusted_dir, trade.symbol, adjusted=True),
                _load_price_file(raw_dir, trade.symbol, adjusted=False),
            )
        adjusted, raw = cache[trade.symbol]
        filename = "{:04d}_{}_{}_{}.png".format(
            int(trade.trade_no), trade.symbol, int(trade.buy_date), int(trade.sell_date))
        render_trade_chart(
            trade, adjusted, raw, output_dir / "charts" / filename,
            stock_name=str(names.get(trade.symbol, "")), position_events=events,
            before=before, after=after,
        )
    trades.to_csv(output_dir / "trades_visualized.csv", index=False)
    return trades, write_html_index(
        trades, output_dir, "VCP 残差动量策略逐笔交易 K 线复盘")

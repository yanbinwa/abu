#!/usr/bin/env python3
"""Render auditable OPEN/ADD/REDUCE/CLOSE position-add trade reports."""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuTradeVisualization import (
    _configure_chinese_font, build_position_add_markers,
)

STYLES = {
    "OPEN": ("^", "#d92d20", "基础买入"),
    "INCREASE": ("P", "#315efb", "加仓"),
    "REDUCE": ("v", "#f79009", "部分退出"),
    "CLOSE": ("X", "#07845a", "完全退出"),
}


def _read_optional(path):
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _prices(directory, symbol, adjusted=True):
    directory = Path(directory)
    if adjusted:
        matches = sorted(directory.glob(symbol + "_*"))
        if not matches:
            raise FileNotFoundError(symbol)
        path = matches[-1]
    else:
        path = directory / (symbol + ".csv")
        if not path.exists():
            return pd.DataFrame()
    frame = pd.read_csv(path)
    date_column = "date" if "date" in frame else "date_time"
    frame["date"] = pd.to_numeric(
        frame[date_column].astype(str).str.replace("-", ""), errors="coerce")
    for field in ("open", "high", "low", "close", "volume"):
        if field not in frame:
            frame[field] = 0.0 if field == "volume" else np.nan
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    return frame.dropna(subset=["date", "open", "high", "low", "close"])


def _window(prices, start, end, before=30, after=12):
    dates = prices.date.astype(int).to_numpy()
    lo = max(0, int(np.searchsorted(dates, start, side="left")) - before)
    hi = min(len(prices), int(np.searchsorted(dates, end, side="right")) + after)
    return prices.iloc[lo:hi].reset_index(drop=True)


def _adjusted_marker_y(row, adjusted, raw):
    adjusted_row = adjusted[adjusted.date.astype(int).eq(int(row.date))]
    if adjusted_row.empty:
        return np.nan
    adjusted_close = float(adjusted_row.iloc[0].close)
    if raw.empty:
        return adjusted_close
    raw_row = raw[raw.date.astype(int).eq(int(row.date))]
    if raw_row.empty or float(raw_row.iloc[0].close) <= 0:
        return adjusted_close
    factor = float(raw_row.iloc[0].close) / adjusted_close
    return float(row.price_raw) / factor if factor > 0 else adjusted_close


def render_trade(markers, adjusted_dir, raw_dir, output, stock_name="",
                 chart_end_date=None):
    _configure_chinese_font()
    markers = markers.sort_values(["date", "fill_allocation_id"]).reset_index(drop=True)
    symbol = str(markers.symbol.iloc[0])
    adjusted = _prices(adjusted_dir, symbol, adjusted=True)
    raw = _prices(raw_dir, symbol, adjusted=False)
    chart_end = (int(chart_end_date) if chart_end_date is not None
                 else int(markers.date.max()))
    frame = _window(adjusted, int(markers.date.min()), chart_end)
    if frame.empty:
        raise ValueError("empty price window for {}".format(symbol))
    frame["datetime"] = pd.to_datetime(frame.date.astype(int).astype(str), format="%Y%m%d")
    date_x = {int(value): index for index, value in enumerate(frame.date)}

    fig = plt.figure(figsize=(15.5, 9.2), constrained_layout=True)
    grid = fig.add_gridspec(3, 1, height_ratios=[5.1, 1.25, 1.7])
    ax = fig.add_subplot(grid[0])
    volume_ax = fig.add_subplot(grid[1], sharex=ax)
    note_ax = fig.add_subplot(grid[2])
    note_ax.axis("off")

    width, candle_colors = .62, []
    for index, price in frame.iterrows():
        color = "#d92d20" if price.close >= price.open else "#07845a"
        candle_colors.append(color)
        ax.vlines(index, price.low, price.high, color=color, linewidth=.9)
        bottom = min(price.open, price.close)
        height = max(abs(price.close-price.open), max(price.close, 1.0)*.0005)
        ax.add_patch(Rectangle((index-width/2, bottom), width, height,
                               facecolor=color, edgecolor=color, linewidth=.65))
    close = frame.close.astype(float)
    ax.plot(close.rolling(20).mean(), color="#f79009", linewidth=1.15, label="MA20")
    ax.plot(close.rolling(60, min_periods=20).mean(), color="#667eea",
            linewidth=1.0, label="MA60")

    operations = []
    for operation_no, row in enumerate(markers.itertuples(index=False), 1):
        x = date_x.get(int(row.date))
        if x is None:
            continue
        y = _adjusted_marker_y(row, adjusted, raw)
        shape, color, label = STYLES.get(
            row.marker_type, ("o", "#667085", row.marker_type))
        ax.axvline(x, color=color, linewidth=.8, alpha=.22, linestyle=":")
        ax.scatter([x], [y], marker=shape, color=color, s=145,
                   edgecolor="white", linewidth=1.0, zorder=7)
        above = row.marker_type in ("OPEN", "INCREASE")
        offset = 28 if above else -38
        ax.annotate("{}  {}".format(operation_no, label), (x, y),
                    xytext=(0, offset), textcoords="offset points", ha="center",
                    fontsize=9, fontweight="bold", color=color,
                    arrowprops={"arrowstyle": "-", "color": color, "alpha": .7},
                    bbox={"boxstyle": "round,pad=.25", "facecolor": "white",
                          "edgecolor": color, "alpha": .92})
        operations.append(row)

    title_name = "{} {}".format(symbol, stock_name).strip()
    status = ("持仓中（按回测期末展示）" if chart_end > int(markers.date.max())
              else "已退出")
    ax.set_title("{}｜{}｜{} 次账本操作｜{} → {}".format(
        title_name, status, len(operations), int(markers.date.min()), chart_end),
        fontsize=14, loc="left")
    ax.set_ylabel("前复权价格")
    ax.grid(axis="y", alpha=.15)
    handles = [plt.Line2D([], [], marker=value[0], color="none",
                          markerfacecolor=value[1], markeredgecolor="white",
                          markersize=10, label=value[2])
               for value in STYLES.values()]
    handles += list(ax.get_legend_handles_labels()[0])
    ax.legend(handles=handles, loc="upper left", ncol=6, fontsize=8)
    plt.setp(ax.get_xticklabels(), visible=False)

    volumes = frame.volume.fillna(0).astype(float).to_numpy()
    volume_ax.bar(np.arange(len(frame)), volumes, width=width,
                  color=candle_colors, alpha=.5)
    volume_ax.set_ylabel("成交量")
    volume_ax.grid(axis="y", alpha=.12)
    step = max(1, len(frame)//10)
    ticks = np.arange(0, len(frame), step)
    volume_ax.set_xticks(ticks)
    volume_ax.set_xticklabels(
        [frame.iloc[int(i)].datetime.strftime("%Y-%m-%d") for i in ticks],
        rotation=28, ha="right", fontsize=8)

    log_lines = []
    for index, row in enumerate(operations, 1):
        result = ("；已实现盈亏 {:+,.2f} 元".format(float(row.realized_pnl_cash))
                  if row.marker_type in ("REDUCE", "CLOSE") else "")
        policy = ("；插件 {}".format(row.source_policy_id)
                  if str(row.source_policy_id) else "")
        log_lines.append(
            "{}  {}  {}｜{:,} 股 @ ¥{:.3f}｜{}{}{}".format(
                index, int(row.date), row.action_label, int(row.quantity),
                float(row.price_raw), row.reason, policy, result))
    note_ax.text(.01, .96, "操作原因（编号与图中一致）\n" + "\n".join(log_lines),
                 ha="left", va="top", fontsize=9.4, linespacing=1.45,
                 bbox={"facecolor": "#f8fafc", "edgecolor": "#d0d5dd",
                       "boxstyle": "round,pad=.55"})
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=135, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _number(value):
    value = float(value)
    return value if np.isfinite(value) else None


def write_html(records, output_dir, title):
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    content = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
:root{--bg:#f4f7fb;--panel:#fff;--ink:#172033;--muted:#667085;--line:#e4e7ec;--blue:#315efb;--soft:#eef3ff;--buy:#d92d20;--sell:#07845a;--add:#315efb;--shadow:0 12px 32px rgba(16,24,40,.08)}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}button,input,select{font:inherit}.top{padding:24px 28px 20px;background:linear-gradient(135deg,#101828,#263d7b);color:white}.top h1{margin:0 0 7px;font-size:24px}.top p{margin:0;color:#d0d5dd;font-size:13px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 28px}.stat,.side,.viewer{background:white;border:1px solid var(--line);box-shadow:var(--shadow)}.stat{border-radius:12px;padding:14px 16px}.stat small{color:var(--muted)}.stat strong{display:block;margin-top:4px;font-size:20px}.workspace{display:grid;grid-template-columns:340px minmax(0,1fr);gap:16px;padding:0 28px 28px}.side,.viewer{border-radius:14px;overflow:hidden}.filters{padding:15px;border-bottom:1px solid var(--line)}.filters input,.filters select,.jump select{width:100%;padding:9px 10px;border:1px solid #d0d5dd;border-radius:8px;background:white}.filterrow{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.listhead{display:flex;justify-content:space-between;padding:11px 15px;color:var(--muted);font-size:12px}.list{height:calc(100vh - 350px);min-height:460px;overflow:auto;padding:0 7px 10px}.item{width:100%;border:0;border-radius:9px;background:transparent;padding:10px;text-align:left;cursor:pointer;display:grid;grid-template-columns:38px 1fr auto;gap:7px;align-items:center}.item:hover{background:#f8fafc}.item.active{background:var(--soft);outline:1px solid #c7d7fe}.no,.meta{color:var(--muted);font-size:11px}.symbol{font-weight:650}.count{color:var(--blue);font-weight:700}.viewerbar{display:flex;gap:9px;align-items:center;padding:13px 15px;border-bottom:1px solid var(--line)}.jump{flex:1}.nav,.original{border:1px solid #d0d5dd;border-radius:8px;background:white;padding:9px 12px;color:var(--ink);cursor:pointer;text-decoration:none}.nav:hover,.original:hover{border-color:var(--blue);color:var(--blue)}.chart{min-height:480px;background:#fbfcfe;display:flex;justify-content:center;align-items:center;padding:10px}.chart img{display:block;max-width:100%;max-height:69vh;object-fit:contain;cursor:zoom-in}.detail{border-top:1px solid var(--line);padding:17px}.detail h2{margin:0;font-size:20px}.badges{display:flex;gap:7px;flex-wrap:wrap;margin:9px 0 13px}.badge{background:#f2f4f7;border-radius:999px;padding:4px 9px;font-size:12px}.ops{width:100%;border-collapse:collapse;font-size:13px}.ops th,.ops td{text-align:left;padding:10px 8px;border-top:1px solid var(--line);vertical-align:top}.ops th{color:var(--muted);font-size:11px}.action{font-weight:700;white-space:nowrap}.OPEN{color:var(--buy)}.INCREASE{color:var(--add)}.REDUCE{color:#f79009}.CLOSE{color:var(--sell)}.reason{line-height:1.55;min-width:280px}.reason small{display:block;color:var(--muted)}.positive{color:var(--buy)}.negative{color:var(--sell)}@media(max-width:960px){.stats{grid-template-columns:repeat(2,1fr)}.workspace{grid-template-columns:1fr}.list{height:300px;min-height:0}}@media(max-width:600px){.top,.workspace{padding-left:12px;padding-right:12px}.stats{margin-left:12px;margin-right:12px}.viewerbar{flex-wrap:wrap}.jump{min-width:100%;order:-1}.ops{display:block;overflow-x:auto}}
</style></head><body><header class="top"><h1>__TITLE__</h1><p>图中编号对应下方操作日志。K 线使用前复权价格，日志保留真实成交价、数量、插件和触发原因。</p></header>
<section class="stats"><div class="stat"><small>逻辑交易</small><strong>__TRADES__ 笔</strong></div><div class="stat"><small>账本操作</small><strong>__OPS__ 次</strong></div><div class="stat"><small>基础买入 / 加仓</small><strong>__OPEN__ / __ADD__</strong></div><div class="stat"><small>退出操作</small><strong>__EXIT__ 次</strong></div></section>
<main class="workspace"><aside class="side"><div class="filters"><input id="search" type="search" placeholder="搜索代码、名称、原因或插件"><div class="filterrow"><select id="action"><option value="all">全部操作</option><option value="OPEN_POSITION">仅看期末持仓</option><option value="OPEN">包含基础买入</option><option value="INCREASE">包含加仓</option><option value="REDUCE">包含部分退出</option><option value="CLOSE">包含完全退出</option></select><select id="sort"><option value="time">按首次买入</option><option value="addDesc">按加仓次数</option><option value="pnlDesc">按已实现盈亏</option></select></div></div><div class="listhead"><span>交易列表</span><span id="visible"></span></div><div id="list" class="list"></div></aside>
<section class="viewer"><div class="viewerbar"><button id="prev" class="nav">← 上一笔</button><div class="jump"><select id="select"></select></div><button id="next" class="nav">下一笔 →</button><a id="original" class="original" target="_blank">查看原图</a></div><div class="chart"><img id="image"><span id="empty" hidden>没有符合条件的交易</span></div><div id="detail" class="detail"></div></section></main>
<script id="data" type="application/json">__DATA__</script><script>
const all=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);let shown=[...all],selected=null;const safe=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));const date=v=>String(v).replace(/(\d{4})(\d{2})(\d{2})/,'$1-$2-$3');const money=v=>`${v>=0?'+':'-'}¥${Math.abs(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
function filter(){const q=$('search').value.trim().toLowerCase(),a=$('action').value,s=$('sort').value;shown=all.filter(t=>(!q||`${t.symbol} ${t.stockName} ${t.searchText}`.toLowerCase().includes(q))&&(a==='all'||(a==='OPEN_POSITION'?t.isOpen:t.operations.some(o=>o.type===a))));if(s==='addDesc')shown.sort((x,y)=>y.addCount-x.addCount);else if(s==='pnlDesc')shown.sort((x,y)=>y.realizedPnl-x.realizedPnl);else shown.sort((x,y)=>x.firstDate-y.firstDate);controls();select((selected&&shown.find(t=>t.tradeNo===selected.tradeNo))||shown[0]||null)}
function controls(){$('visible').textContent=`${shown.length} / ${all.length}`;$('list').innerHTML=shown.map(t=>`<button class="item ${selected&&selected.tradeNo===t.tradeNo?'active':''}" data-id="${t.tradeNo}"><span class="no">#${t.tradeNo}</span><span><span class="symbol">${safe(t.symbol)} ${safe(t.stockName)}</span><span class="meta">${date(t.firstDate)} · ${safe(t.statusLabel)} · ${t.operations.length}次操作</span></span><span class="count">${t.isOpen?'持仓中':money(t.realizedPnl)}</span></button>`).join('');$('select').innerHTML=shown.map(t=>`<option value="${t.tradeNo}">#${t.tradeNo} · ${safe(t.symbol)} ${safe(t.stockName)} · ${safe(t.statusLabel)}</option>`).join('');document.querySelectorAll('.item').forEach(x=>x.onclick=()=>select(all.find(t=>t.tradeNo===Number(x.dataset.id))))}
function select(t){selected=t;const yes=Boolean(t);$('image').hidden=!yes;$('empty').hidden=yes;$('detail').hidden=!yes;$('original').hidden=!yes;if(!yes){$('prev').disabled=$('next').disabled=true;return}const i=shown.findIndex(x=>x.tradeNo===t.tradeNo);$('select').value=t.tradeNo;$('image').src=t.image;$('image').alt=`${t.symbol} 交易 K 线`;$('original').href=t.image;$('prev').disabled=i<=0;$('next').disabled=i<0||i>=shown.length-1;const rows=t.operations.map(o=>`<tr><td><b>${o.no}</b></td><td>${date(o.date)}</td><td class="action ${o.type}">${safe(o.action)}</td><td>${o.quantity.toLocaleString()}股<br><small>¥${o.price.toFixed(3)}</small></td><td class="reason">${safe(o.reason)}<small>${safe(o.detail)}</small>${o.policy?`<small>插件：${safe(o.policy)} ${safe(o.policyVersion)}</small>`:''}</td><td class="${o.realizedPnl>=0?'positive':'negative'}">${o.type==='REDUCE'||o.type==='CLOSE'?money(o.realizedPnl):'—'}</td></tr>`).join('');$('detail').innerHTML=`<h2>#${t.tradeNo} ${safe(t.symbol)} ${safe(t.stockName)}</h2><div class="badges"><span class="badge">${safe(t.statusLabel)}</span><span class="badge">${date(t.firstDate)} → ${date(t.lastDate)}</span><span class="badge">基础买入 ${t.openCount} 次</span><span class="badge">加仓 ${t.addCount} 次</span><span class="badge">已实现盈亏 ${money(t.realizedPnl)}</span></div><table class="ops"><thead><tr><th>#</th><th>日期</th><th>操作</th><th>成交</th><th>操作原因</th><th>已实现盈亏</th></tr></thead><tbody>${rows}</tbody></table>`;history.replaceState(null,'',`#trade=${t.tradeNo}`);controls();setTimeout(()=>document.querySelector(`.item[data-id="${t.tradeNo}"]`)?.scrollIntoView({block:'nearest'}),0)}
function move(d){if(!selected)return;const i=shown.findIndex(x=>x.tradeNo===selected.tradeNo);if(shown[i+d])select(shown[i+d])}$('search').oninput=filter;$('action').onchange=filter;$('sort').onchange=filter;$('select').onchange=e=>select(all.find(t=>t.tradeNo===Number(e.target.value)));$('prev').onclick=()=>move(-1);$('next').onclick=()=>move(1);$('image').onclick=()=>selected&&window.open(selected.image,'_blank');document.addEventListener('keydown',e=>{if(e.target.matches('input,select'))return;if(e.key==='ArrowLeft')move(-1);if(e.key==='ArrowRight')move(1)});selected=all.find(t=>t.tradeNo===Number(location.hash.match(/trade=(\d+)/)?.[1]))||all[0]||null;filter();
</script></body></html>'''
    counts = {
        "__TITLE__": html.escape(title), "__TRADES__": str(len(records)),
        "__OPS__": str(sum(len(row["operations"]) for row in records)),
        "__OPEN__": str(sum(row["openCount"] for row in records)),
        "__ADD__": str(sum(row["addCount"] for row in records)),
        "__EXIT__": str(sum(sum(op["type"] in ("REDUCE", "CLOSE")
                                for op in row["operations"]) for row in records)),
        "__DATA__": payload,
    }
    for key, value in counts.items():
        content = content.replace(key, value)
    path = Path(output_dir) / "index.html"
    path.write_text(content, encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--adjusted-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/raw"))
    parser.add_argument("--security-master", type=Path,
                        default=Path("/Users/wjy/abu/data/cache/akshare_cn_stock_info.csv"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--title", default="加仓策略逐笔 K 线复盘")
    parser.add_argument("--skip-charts", action="store_true",
                        help="reuse existing PNG files and rebuild only CSV/HTML")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    charts = args.output_dir / "charts"
    charts.mkdir(exist_ok=True)

    fills = pd.read_csv(args.backtest_dir / "physical_fills.csv")
    allocations = pd.read_csv(args.backtest_dir / "fill_allocations.csv")
    dispositions = pd.read_csv(args.backtest_dir / "lot_dispositions.csv")
    markers = build_position_add_markers(
        fills, allocations, dispositions,
        orders=_read_optional(args.backtest_dir / "logical_orders.csv"),
        evaluations=_read_optional(args.backtest_dir / "policy_evaluations.csv"),
        proposals=_read_optional(args.backtest_dir / "add_proposals.csv"),
        exit_reasons=_read_optional(args.backtest_dir / "exit_reasons.csv"),
    )
    daily_nav = _read_optional(args.backtest_dir / "daily_nav.csv")
    backtest_end = (int(pd.to_numeric(daily_nav.date).max())
                    if len(daily_nav) and "date" in daily_nav else None)
    names = {}
    if args.security_master.exists():
        master = pd.read_csv(args.security_master, dtype=str)
        names = dict(zip(master.symbol, master.name))

    records, csv_rows = [], []
    for number, (trade_id, group) in enumerate(
            markers.groupby("trade_id", sort=True), 1):
        group = group.sort_values(["date", "fill_allocation_id"]).reset_index(drop=True)
        symbol = str(group.symbol.iloc[0])
        is_open = not group.marker_type.eq("CLOSE").any()
        display_end = (backtest_end if is_open and backtest_end is not None
                       else int(group.date.max()))
        path = charts / ("{:04d}_{}.png".format(number, trade_id))
        if not args.skip_charts or not path.exists():
            render_trade(group, args.adjusted_dir, args.raw_dir, path,
                         stock_name=names.get(symbol, ""),
                         chart_end_date=display_end if is_open else None)
        operations = []
        for operation_no, row in enumerate(group.itertuples(index=False), 1):
            operations.append({
                "no": operation_no, "date": int(row.date),
                "type": str(row.marker_type), "action": str(row.action_label),
                "quantity": int(row.quantity), "price": _number(row.price_raw),
                "reasonCode": str(row.reason_code), "reason": str(row.reason),
                "detail": str(row.reason_detail),
                "policy": str(row.source_policy_id),
                "policyVersion": str(row.source_policy_version),
                "fees": _number(row.fees_cash),
                "realizedPnl": _number(row.realized_pnl_cash),
            })
        realized = float(group.realized_pnl_cash.sum())
        record = {
            "tradeNo": number, "tradeId": str(trade_id), "symbol": symbol,
            "stockName": names.get(symbol, ""),
            "firstDate": int(group.date.min()), "lastDate": display_end,
            "isOpen": bool(is_open),
            "statusLabel": "期末持仓" if is_open else "已平仓",
            "openCount": int((group.marker_type == "OPEN").sum()),
            "addCount": int((group.marker_type == "INCREASE").sum()),
            "realizedPnl": realized, "operations": operations,
            "searchText": " ".join(str(value) for value in
                                   group[["reason", "reason_detail",
                                          "source_policy_id"]].to_numpy().ravel()),
            "image": "charts/" + path.name,
        }
        records.append(record)
        csv_rows.append({
            "trade_no": number, "trade_id": trade_id, "symbol": symbol,
            "stock_name": names.get(symbol, ""), "operations": len(group),
            "open_count": record["openCount"], "add_count": record["addCount"],
            "status": "OPEN" if is_open else "CLOSED",
            "realized_pnl_cash": realized, "chart": record["image"],
        })
    pd.DataFrame(csv_rows).to_csv(args.output_dir / "trades_visualized.csv", index=False)
    markers.to_csv(args.output_dir / "operations_visualized.csv", index=False)
    index = write_html(records, args.output_dir, args.title)
    manifest = {
        "trades": len(records), "markers": len(markers),
        "open_trades": sum(record["isOpen"] for record in records),
        "closed_trades": sum(not record["isOpen"] for record in records),
        "operations_with_reason": int(markers.reason.astype(str).str.len().gt(0).sum()),
        "unrecorded_exit_reasons": int(markers.reason_code.eq(
            "UNRECORDED_EXIT_REASON").sum()), "index": str(index),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()

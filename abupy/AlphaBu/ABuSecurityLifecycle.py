# -*- encoding: utf-8 -*-
"""Point-in-time security lifecycle events and conservative valuation helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd


EVENT_TYPES = {
    "LISTED", "ST_ENTER", "ST_EXIT", "SUSPENDED", "RESUMED",
    "DELISTING_PERIOD_START", "TERMINATED", "CASH_ACQUISITION",
    "STOCK_SWAP", "CASH_DIVIDEND", "STOCK_DIVIDEND",
    "SPLIT_OR_CONSOLIDATION",
}


def date_number(value) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else int(parsed.strftime("%Y%m%d"))


@dataclass(frozen=True)
class SecurityLifecycleEvent:
    symbol: str
    event_type: str
    effective_date: int
    announcement_date: int | None = None
    record_date: int | None = None
    cash_per_share: float = 0.0
    share_ratio: float = 0.0
    replacement_symbol: str | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.event_type not in EVENT_TYPES:
            raise ValueError("unknown lifecycle event: {}".format(self.event_type))
        if self.effective_date <= 0:
            raise ValueError("effective_date must be a YYYYMMDD integer")

    def visible_on(self, asof: int) -> bool:
        known = self.announcement_date or self.effective_date
        return known <= int(asof)


def build_master_events(master: pd.DataFrame) -> list[SecurityLifecycleEvent]:
    events = []
    for row in master.to_dict("records"):
        symbol = str(row.get("symbol", ""))
        listed = date_number(row.get("list_date"))
        delisted = date_number(row.get("delist_date"))
        if symbol and listed:
            events.append(SecurityLifecycleEvent(
                symbol=symbol, event_type="LISTED", effective_date=listed,
                announcement_date=listed, source="security_master",
            ))
        if symbol and delisted:
            events.append(SecurityLifecycleEvent(
                symbol=symbol, event_type="TERMINATED", effective_date=delisted,
                announcement_date=None, source="security_master",
            ))
    return sorted(events, key=lambda item: (item.effective_date, item.symbol,
                                             item.event_type))


def build_corporate_action_events(frame: pd.DataFrame) -> list[SecurityLifecycleEvent]:
    """Normalize the currently available CNINFO/Sina action columns."""
    if frame is None or frame.empty:
        return []
    events = []
    for row in frame.to_dict("records"):
        symbol = str(row.get("symbol", ""))
        announcement = date_number(row.get("实施方案公告日期"))
        record = date_number(row.get("股权登记日"))
        ex_date = date_number(row.get("除权日"))
        cash_date = date_number(row.get("派息日")) or ex_date
        stock_date = date_number(row.get("股份到账日")) or ex_date
        cash = pd.to_numeric(row.get("派息比例"), errors="coerce")
        sent = pd.to_numeric(row.get("送股比例"), errors="coerce")
        converted = pd.to_numeric(row.get("转增比例"), errors="coerce")
        stock = ((0.0 if pd.isna(sent) else float(sent)) +
                 (0.0 if pd.isna(converted) else float(converted)))
        cash = 0.0 if pd.isna(cash) else float(cash) / 10.0
        stock = float(stock) / 10.0
        metadata = {"description": row.get("实施方案分红说明", "")}
        if symbol and cash and cash_date:
            events.append(SecurityLifecycleEvent(
                symbol=symbol, event_type="CASH_DIVIDEND",
                effective_date=cash_date, announcement_date=announcement,
                record_date=record, cash_per_share=cash,
                source="corporate_actions", metadata=metadata,
            ))
        if symbol and stock and stock_date:
            events.append(SecurityLifecycleEvent(
                symbol=symbol, event_type="STOCK_DIVIDEND",
                effective_date=stock_date, announcement_date=announcement,
                record_date=record, share_ratio=stock,
                source="corporate_actions", metadata=metadata,
            ))
    return sorted(events, key=lambda item: (item.effective_date, item.symbol,
                                             item.event_type))


def build_name_change_events(frame: pd.DataFrame, exchange="sz"):
    if frame is None or frame.empty:
        return []
    required = {"证券代码", "变更日期", "变更后简称"}
    if not required.issubset(frame.columns):
        return []
    events = []
    work = frame.copy()
    work["symbol"] = exchange + work["证券代码"].astype(str).str.zfill(6)
    work["effective"] = work["变更日期"].map(date_number)
    for symbol, group in work.dropna(subset=["effective"]).groupby("symbol"):
        previous_st = False
        for row in group.sort_values("effective").to_dict("records"):
            name = str(row.get("变更后简称", "")).upper().replace(" ", "")
            current_st = name.startswith(("ST", "*ST", "S*ST", "SST"))
            if current_st != previous_st:
                events.append(SecurityLifecycleEvent(
                    symbol=symbol,
                    event_type="ST_ENTER" if current_st else "ST_EXIT",
                    effective_date=int(row["effective"]),
                    announcement_date=int(row["effective"]),
                    source="name_changes",
                    metadata={"name": row.get("变更后简称", "")},
                ))
            previous_st = current_st
    return sorted(events, key=lambda item: (item.effective_date, item.symbol,
                                             item.event_type))


def build_suspension_events(dates, symbols, suspended_mask, universe_mask):
    dates = np.asarray(dates, dtype=np.int64)
    suspended = np.asarray(suspended_mask, dtype=bool)
    universe = np.asarray(universe_mask, dtype=bool)
    if suspended.shape != universe.shape or suspended.shape != (len(dates), len(symbols)):
        raise ValueError("suspension matrices have wrong shape")
    events = []
    for column, symbol in enumerate(symbols):
        previous = False
        for row, date in enumerate(dates):
            current = bool(suspended[row, column] and universe[row, column])
            if current != previous:
                events.append(SecurityLifecycleEvent(
                    symbol=symbol,
                    event_type="SUSPENDED" if current else "RESUMED",
                    effective_date=int(date), announcement_date=int(date),
                    source="raw_daily_bars",
                ))
            previous = current
    return events


def events_visible_asof(events: Iterable[SecurityLifecycleEvent], asof: int):
    return [event for event in events if event.visible_on(asof)]


def accounting_mark(observed_close: float, last_valid_close: float,
                    terminated: bool, recovery_per_share: float | None = None) -> float:
    """Return a deterministic book mark without inventing a future recovery."""
    if terminated:
        return max(0.0, float(recovery_per_share or 0.0))
    if np.isfinite(observed_close) and observed_close > 0:
        return float(observed_close)
    if np.isfinite(last_valid_close) and last_valid_close > 0:
        return float(last_valid_close)
    return 0.0


def liquidation_mark(mark: float, limit_fraction: float, sessions: int) -> float:
    if mark < 0 or not 0 <= limit_fraction < 1 or sessions < 0:
        raise ValueError("invalid liquidation mark parameters")
    return float(mark) * ((1.0 - float(limit_fraction)) ** int(sessions))

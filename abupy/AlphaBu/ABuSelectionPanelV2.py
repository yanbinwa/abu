# -*- encoding: utf-8 -*-
"""Point-in-time panel semantics layered over the frozen v1 price matrices."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .ABuSecurityLifecycle import (
    build_corporate_action_events, build_master_events,
    build_name_change_events, build_suspension_events,
)
from .ABuSelectionStrategies import SelectionPanel


MASK_REASON_CODES = (
    "NOT_LISTED", "AFTER_DELIST_DATE", "UNKNOWN_ST_STATUS",
    "KNOWN_ST", "INSUFFICIENT_HISTORY", "MISSING_SIGNAL_PRICE",
    "MISSING_RAW_PRICE", "MISSING_AMOUNT", "MISSING_TURNOVER",
    "SUSPENDED", "LONG_SUSPENSION", "TERMINATED",
)


def _date_ints(values, missing_default):
    parsed = pd.to_datetime(values, errors="coerce")
    numbers = pd.to_numeric(parsed.dt.strftime("%Y%m%d"), errors="coerce")
    return numbers.fillna(missing_default).to_numpy(dtype=np.int64)


def _infer_board(symbol: str) -> str:
    code = symbol[2:]
    if symbol.startswith("sh") and code.startswith(("688", "689")):
        return "star"
    if symbol.startswith("sz") and code.startswith(("300", "301")):
        return "chinext"
    return "main"


def _consecutive_true(values: np.ndarray) -> np.ndarray:
    result = np.zeros(values.shape, dtype=np.int16)
    for row in range(values.shape[0]):
        if row == 0:
            result[row] = values[row].astype(np.int16)
        else:
            result[row] = np.where(values[row], result[row - 1] + 1, 0)
    return result


class SelectionPanelV2(object):
    """Adds PIT lifecycle and field-availability masks to SelectionPanel v1."""

    def __init__(self, base: SelectionPanel, master: pd.DataFrame,
                 turnover=None, st_status_known=None, lifecycle_events=None,
                 long_suspension_sessions=20):
        self.base = base
        self.dates = base.dates
        self.symbols = base.symbols
        self.symbol_index = {symbol: position
                             for position, symbol in enumerate(self.symbols)}
        shape = (len(self.dates), len(self.symbols))
        self.turnover = (np.full(shape, np.nan, dtype=np.float32)
                         if turnover is None else np.asarray(turnover, dtype=np.float32))
        if self.turnover.shape != shape:
            raise ValueError("turnover has wrong shape")

        master = master.copy()
        if "symbol" not in master:
            raise ValueError("security master requires symbol")
        master["symbol"] = master.symbol.astype(str)
        master = master.drop_duplicates("symbol", keep="first").set_index("symbol")
        list_date = np.zeros(len(self.symbols), dtype=np.int64)
        delist_date = np.full(len(self.symbols), 99991231, dtype=np.int64)
        status = np.full(len(self.symbols), "unknown", dtype=object)
        boards = []
        for column, symbol in enumerate(self.symbols):
            boards.append(_infer_board(symbol))
            if symbol not in master.index:
                continue
            row = master.loc[symbol]
            list_date[column] = _date_ints(pd.Series([row.get("list_date")]), 0)[0]
            delist_date[column] = _date_ints(
                pd.Series([row.get("delist_date")]), 99991231
            )[0]
            status[column] = str(row.get("status", "unknown"))
        self.list_date = list_date
        self.delist_date = delist_date
        self.security_status = status
        self.board = np.asarray(boards, dtype=object)

        dates = self.dates[:, None]
        self.universe_mask = ((dates >= list_date[None, :]) &
                              (dates <= delist_date[None, :]) &
                              (list_date[None, :] > 0))
        self.terminated_mask = dates > delist_date[None, :]

        if st_status_known is None:
            # Shenzhen name-change history is available; Shanghai history is
            # incomplete and therefore remains unknown in strict PIT mode.
            known_symbols = np.array(
                [symbol.startswith("sz") for symbol in self.symbols], dtype=bool
            )
            st_status_known = np.broadcast_to(known_symbols, shape).copy()
        self.st_status_known = np.asarray(st_status_known, dtype=bool)
        if self.st_status_known.shape != shape:
            raise ValueError("st_status_known has wrong shape")

        self.signal_price_available = (
            np.isfinite(base.close) & (base.close > 0) &
            np.isfinite(base.volume)
        )
        self.raw_price_available = (
            np.isfinite(base.exec_open) & (base.exec_open > 0) &
            np.isfinite(base.exec_close) & (base.exec_close > 0)
        )
        self.suspended_mask = (
            ~self.raw_price_available | ~np.isfinite(base.exec_volume) |
            (base.exec_volume <= 0)
        ) & self.universe_mask
        self.stale_days = _consecutive_true(self.suspended_mask)
        self.long_suspension_mask = (
            self.stale_days > int(long_suspension_sessions)
        ) & self.universe_mask

        history = np.cumsum(self.signal_price_available & self.universe_mask, axis=0)
        self.history_count = history.astype(np.int16)
        self.data_available_mask = (
            self.signal_price_available & self.raw_price_available
        )
        self.buy_tradable_mask = (
            self.raw_price_available & np.isfinite(base.exec_volume) &
            (base.exec_volume > 0) & self.universe_mask
        )
        self.sell_tradable_mask = self.buy_tradable_mask.copy()
        self.lifecycle_events = lifecycle_events or []

    def __getattr__(self, name):
        # During checkpoint restoration ``base`` is not assigned yet. Avoid
        # recursively invoking this fallback while pickle checks __setstate__.
        return getattr(object.__getattribute__(self, "base"), name)

    @classmethod
    def from_research_data(cls, signal_dir, research_dir, start_date=20200101,
                           end_date=20261002, long_suspension_sessions=20,
                           st_exclusion_panel=None):
        signal_dir = Path(signal_dir)
        research_dir = Path(research_dir)
        base = SelectionPanel.from_research_data(
            signal_dir, research_dir, start_date=start_date, end_date=end_date
        )
        master = pd.read_csv(
            research_dir / "security_master.csv", dtype={"code": str, "symbol": str}
        )
        calendar_index = pd.Index(base.dates)
        turnover = np.full((len(base.dates), len(base.symbols)), np.nan,
                           dtype=np.float32)
        raw_dir = research_dir / "raw"
        for column, symbol in enumerate(base.symbols):
            path = raw_dir / (symbol + ".csv")
            if not path.exists() or "turnover" not in pd.read_csv(path, nrows=0).columns:
                continue
            frame = pd.read_csv(path, usecols=["date", "turnover"])
            rows = calendar_index.get_indexer(
                pd.to_numeric(frame.date, errors="coerce").fillna(0).astype(int)
            )
            valid = rows >= 0
            turnover[rows[valid], column] = pd.to_numeric(
                frame.turnover, errors="coerce"
            ).to_numpy(dtype=np.float32)[valid]

        events = build_master_events(master)
        action_path = research_dir / "corporate_actions.csv"
        if action_path.exists():
            events.extend(build_corporate_action_events(
                pd.read_csv(action_path, dtype={"symbol": str})
            ))
        name_path = research_dir / "sz_name_changes.csv"
        if name_path.exists():
            events.extend(build_name_change_events(
                pd.read_csv(name_path, dtype={"证券代码": str}), exchange="sz"
            ))
        st_status_known = None
        if st_exclusion_panel is not None:
            st_status, st_status_known = cls._load_st_exclusion_panel(
                st_exclusion_panel, base.dates, base.symbols)
            base.st_status = st_status
        panel = cls(base, master, turnover=turnover,
                    st_status_known=st_status_known,
                    lifecycle_events=events,
                    long_suspension_sessions=long_suspension_sessions)
        panel.lifecycle_events.extend(build_suspension_events(
            panel.dates, panel.symbols, panel.suspended_mask,
            panel.universe_mask,
        ))
        panel.lifecycle_events.sort(
            key=lambda item: (item.effective_date, item.symbol, item.event_type)
        )
        return panel

    @staticmethod
    def _load_st_exclusion_panel(path, dates, symbols):
        with np.load(Path(path), allow_pickle=False) as payload:
            source_role = payload["source_role"].tolist()
            if source_role != ["st_exclusion_only"]:
                raise ValueError("ST panel is not exclusion-only")
            source_dates = payload["dates"].astype(np.int64)
            source_symbols = payload["symbols"].astype(str)
            known = payload["known"].astype(bool)
            is_st = payload["is_st"].astype(bool)
        expected = (len(source_dates), len(source_symbols))
        if known.shape != expected or is_st.shape != expected:
            raise ValueError("ST exclusion panel has inconsistent shape")
        if np.any(is_st & ~known):
            raise ValueError("ST exclusion panel marks unknown state as ST")
        if len(np.unique(source_dates)) != len(source_dates) or \
                len(np.unique(source_symbols)) != len(source_symbols):
            raise ValueError("ST exclusion panel has duplicate axes")
        date_index = {int(value): index
                      for index, value in enumerate(source_dates)}
        symbol_index = {str(value): index
                        for index, value in enumerate(source_symbols)}
        output_shape = (len(dates), len(symbols))
        output_known = np.zeros(output_shape, dtype=bool)
        output_st = np.zeros(output_shape, dtype=bool)
        for row, day in enumerate(dates):
            source_row = date_index.get(int(day))
            if source_row is None:
                continue
            for column, symbol in enumerate(symbols):
                source_column = symbol_index.get(str(symbol))
                if source_column is None:
                    continue
                output_known[row, column] = known[source_row, source_column]
                output_st[row, column] = is_st[source_row, source_column]
        return output_st, output_known

    def field_available(self, field: str):
        if field == "amount":
            return np.isfinite(self.base.amount) & (self.base.amount > 0)
        if field == "turnover":
            return np.isfinite(self.turnover) & (self.turnover >= 0)
        if field == "market_cap":
            return np.isfinite(self.base.market_cap) & (self.base.market_cap > 0)
        if hasattr(self.base, field):
            values = np.asarray(getattr(self.base, field))
            if values.shape == self.universe_mask.shape:
                return np.isfinite(values)
        raise KeyError("unknown panel field: {}".format(field))

    def signal_eligible(self, min_history=120, required_fields=(),
                        unknown_st_policy="exclude"):
        eligible = (self.universe_mask & self.data_available_mask &
                    (self.history_count >= int(min_history)) &
                    ~self.base.st_status)
        if unknown_st_policy == "exclude":
            eligible &= self.st_status_known
        elif unknown_st_policy != "include":
            raise ValueError("unknown ST policy: {}".format(unknown_st_policy))
        for field in required_fields:
            eligible &= self.field_available(field)
        return eligible

    def breadth_denominator(self, min_history=120, unknown_st_policy="exclude"):
        result = (self.universe_mask &
                  (self.history_count >= int(min_history)) &
                  ~self.long_suspension_mask &
                  ~self.base.st_status)
        if unknown_st_policy == "exclude":
            result &= self.st_status_known
        elif unknown_st_policy != "include":
            raise ValueError("unknown ST policy: {}".format(unknown_st_policy))
        return result

    def eligibility_reasons(self, day, symbol, min_history=120,
                            required_fields=(), unknown_st_policy="exclude"):
        reasons = []
        if not self.universe_mask[day, symbol]:
            if self.dates[day] < self.list_date[symbol]:
                reasons.append("NOT_LISTED")
            elif self.dates[day] > self.delist_date[symbol]:
                reasons.extend(("AFTER_DELIST_DATE", "TERMINATED"))
        if unknown_st_policy == "exclude" and not self.st_status_known[day, symbol]:
            reasons.append("UNKNOWN_ST_STATUS")
        if self.base.st_status[day, symbol]:
            reasons.append("KNOWN_ST")
        if self.history_count[day, symbol] < min_history:
            reasons.append("INSUFFICIENT_HISTORY")
        if not self.signal_price_available[day, symbol]:
            reasons.append("MISSING_SIGNAL_PRICE")
        if not self.raw_price_available[day, symbol]:
            reasons.append("MISSING_RAW_PRICE")
        if self.suspended_mask[day, symbol]:
            reasons.append("SUSPENDED")
        if self.long_suspension_mask[day, symbol]:
            reasons.append("LONG_SUSPENSION")
        for field in required_fields:
            if not self.field_available(field)[day, symbol]:
                reasons.append({
                    "amount": "MISSING_AMOUNT",
                    "turnover": "MISSING_TURNOVER",
                }.get(field, "MISSING_{}".format(field.upper())))
        return tuple(dict.fromkeys(reasons))

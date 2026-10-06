# -*- encoding: utf-8 -*-
"""Retrospective close-event features with explicit non-PIT provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


DATASET_VERSION = "eltdx_close_history_backfill_v1"


class MarketSentimentEntryGate:
    """Suppress new review entries on a frozen, date-level risk-off signal.

    The overlay deliberately leaves rank exits untouched.  It is compatible
    with ``run_low_turnover``'s review hook and records every evaluation so an
    apparent portfolio improvement can be tied back to an actual intervention.
    """

    def __init__(self, daily_factors, sentiment_max=-1.0, risk_min=1.0):
        required = {"signal_asof", "sentiment_z63", "risk_z63"}
        missing = required - set(daily_factors.columns)
        if missing:
            raise ValueError("market gate fields missing: {}".format(
                ",".join(sorted(missing))))
        if daily_factors.signal_asof.duplicated().any():
            raise ValueError("market gate dates must be unique")
        self.sentiment_max = float(sentiment_max)
        self.risk_min = float(risk_min)
        self._by_date = daily_factors.set_index("signal_asof")
        self.evaluations = []

    def _risk_off(self, row):
        return (
            float(row.sentiment_z63) <= self.sentiment_max and
            float(row.risk_z63) >= self.risk_min)

    def _extra_evaluation_fields(self, row):
        return {}

    def trigger_dates(self):
        return [int(date) for date, row in self._by_date.iterrows()
                if self._risk_off(row)]

    def filter_review(self, panel, executor, day, rank_exits, entry_symbols):
        signal_asof = int(panel.dates[day])
        if signal_asof not in self._by_date.index:
            raise KeyError("market gate date is not complete: {}".format(
                signal_asof))
        row = self._by_date.loc[signal_asof]
        risk_off = self._risk_off(row)
        entries = list(entry_symbols)
        exits = list(rank_exits)
        evaluation = {
            "signal_asof": signal_asof,
            "sentiment_z63": float(row.sentiment_z63),
            "risk_z63": float(row.risk_z63),
            "risk_off": bool(risk_off),
            "requested_entries": int(len(entries)),
            "suppressed_entries": int(len(entries) if risk_off else 0),
            "rank_exits_preserved": int(len(exits)),
        }
        evaluation.update(self._extra_evaluation_fields(row))
        self.evaluations.append(evaluation)
        return exits, ([] if risk_off else entries)


class MarketSentimentTransitionEntryGate(MarketSentimentEntryGate):
    """Block only when an already weak state deteriorates versus prior day."""

    def __init__(self, daily_factors, sentiment_max=-1.0, risk_min=1.0):
        super().__init__(daily_factors, sentiment_max, risk_min)
        self._by_date = self._by_date.sort_index()
        self._by_date["sentiment_z63_previous"] = \
            self._by_date.sentiment_z63.shift(1)
        self._by_date["risk_z63_previous"] = self._by_date.risk_z63.shift(1)

    def _risk_off(self, row):
        previous = (float(row.sentiment_z63_previous),
                    float(row.risk_z63_previous))
        if not np.isfinite(previous).all():
            return False
        return (super()._risk_off(row) and
                float(row.sentiment_z63) < previous[0] and
                float(row.risk_z63) > previous[1])

    def _extra_evaluation_fields(self, row):
        return {
            "sentiment_z63_previous": float(row.sentiment_z63_previous),
            "risk_z63_previous": float(row.risk_z63_previous),
            "sentiment_z63_delta_1d": float(
                row.sentiment_z63 - row.sentiment_z63_previous),
            "risk_z63_delta_1d": float(
                row.risk_z63 - row.risk_z63_previous),
        }


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _symbol(code):
    value = str(code).strip().split(".")[0].zfill(6)
    if value.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh" + value
    if value.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return "sz" + value
    if value.startswith(("4", "8", "92")):
        return "bj" + value
    return ""


def complete_history_manifests(root, start_date=None, end_date=None):
    """Select one immutable complete batch per month and verify its payload."""
    root = Path(root)
    rows = []
    for month_root in sorted((root / "batches").glob("[0-9]" * 6)):
        complete = []
        for path in sorted(month_root.glob("*/manifest.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") == "complete":
                complete.append((path, payload))
        if not complete:
            continue
        path, payload = complete[-1]
        if payload.get("dataset_version") != DATASET_VERSION:
            raise ValueError("history dataset version mismatch: {}".format(path))
        parsed = Path(payload["parsed_path"])
        raw = Path(payload["raw_path"])
        if (_sha256(parsed) != payload["parsed_sha256"] or
                _sha256(raw) != payload["raw_sha256"]):
            raise ValueError("history payload hash mismatch: {}".format(path))
        if start_date is not None and int(payload["query_end"]) < int(start_date):
            continue
        if end_date is not None and int(payload["query_start"]) > int(end_date):
            continue
        rows.append(payload)
    return rows


def load_complete_history(root, start_date=None, end_date=None):
    manifests = complete_history_manifests(root, start_date, end_date)
    frames, sessions = [], []
    for manifest in manifests:
        frame = pd.read_csv(
            manifest["parsed_path"], dtype={"code": str,
                                             "trading_date_value": str})
        frames.append(frame)
        sessions.extend(int(value) for value in manifest["expected_sessions"])
    if not frames:
        return pd.DataFrame(), [], manifests
    events = pd.concat(frames, ignore_index=True)
    events["trade_date"] = pd.to_numeric(
        events.trading_date_value, errors="raise").astype(int)
    if start_date is not None:
        events = events[events.trade_date >= int(start_date)]
        sessions = [value for value in sessions if value >= int(start_date)]
    if end_date is not None:
        events = events[events.trade_date <= int(end_date)]
        sessions = [value for value in sessions if value <= int(end_date)]
    events["symbol"] = events.code.map(_symbol)
    if events.symbol.eq("").any():
        raise ValueError("unmapped security code in close-event history")
    return events.reset_index(drop=True), sorted(set(sessions)), manifests


def _rolling_z(values, window=63, minimum=20):
    values = pd.Series(values, dtype=float)
    mean = values.rolling(window, min_periods=minimum).mean()
    std = values.rolling(window, min_periods=minimum).std(ddof=0)
    return ((values - mean) / std.replace(0, np.nan)).fillna(0.0)


def build_close_event_features(events, complete_sessions):
    """Build numeric event features; reasons, names and industries stay excluded."""
    sessions = pd.DataFrame({
        "signal_asof": sorted({int(value) for value in complete_sessions})})
    work = events.copy()
    if work.empty:
        return sessions, pd.DataFrame()
    work["trade_date"] = pd.to_numeric(work.trade_date, errors="raise").astype(int)
    work["board_level"] = pd.to_numeric(
        work.board_level, errors="coerce").fillna(0.0).clip(lower=0)
    work["broken_count"] = pd.to_numeric(
        work.broken_count, errors="coerce").fillna(0.0).clip(lower=0)
    work["seal_amount"] = pd.to_numeric(
        work.seal_amount, errors="coerce").fillna(0.0).clip(lower=0)
    counts = work.pivot_table(
        index="trade_date", columns="status", values="symbol",
        aggfunc="nunique", fill_value=0)
    for status in ("limit_up", "broken", "limit_down"):
        if status not in counts:
            counts[status] = 0
    counts = counts.reset_index().rename(columns={
        "trade_date": "signal_asof", "limit_up": "limit_up_count",
        "broken": "broken_count", "limit_down": "limit_down_count",
    })
    daily = sessions.merge(counts, on="signal_asof", how="left")
    for column in ("limit_up_count", "broken_count", "limit_down_count"):
        daily[column] = daily[column].fillna(0).astype(int)
    upper_attempts = daily.limit_up_count + daily.broken_count
    total = upper_attempts + daily.limit_down_count
    daily["seal_rate"] = np.divide(
        daily.limit_up_count, upper_attempts,
        out=np.zeros(len(daily), dtype=float), where=upper_attempts > 0)
    daily["net_event_breadth"] = np.divide(
        daily.limit_up_count - daily.limit_down_count, total,
        out=np.zeros(len(daily), dtype=float), where=total > 0)
    board = work[work.status.eq("limit_up")].groupby("trade_date").agg(
        highest_board=("board_level", "max"),
        multi_board_count=("board_level", lambda value: int((value >= 2).sum())),
    ).reset_index().rename(columns={"trade_date": "signal_asof"})
    daily = daily.merge(board, on="signal_asof", how="left")
    daily[["highest_board", "multi_board_count"]] = daily[[
        "highest_board", "multi_board_count"]].fillna(0.0)
    sentiment = (np.log1p(daily.limit_up_count) -
                 np.log1p(daily.limit_down_count) + daily.seal_rate)
    risk = np.log1p(daily.limit_down_count + daily.broken_count)
    daily["sentiment_z63"] = _rolling_z(sentiment)
    daily["risk_z63"] = _rolling_z(risk)
    daily["availability_evidence"] = "BACKFILLED_QUERY"
    daily["strict_pit_allowed"] = False

    stock = work[["trade_date", "symbol", "status", "board_level",
                  "broken_count", "seal_amount"]].copy()
    stock["event_limit_up"] = stock.status.eq("limit_up").astype(float)
    stock["event_broken"] = stock.status.eq("broken").astype(float)
    stock["event_limit_down"] = stock.status.eq("limit_down").astype(float)
    stock["event_board_level"] = stock.board_level.astype(float)
    stock["event_broken_count"] = stock.broken_count.astype(float)
    stock["event_log_seal_amount"] = np.log1p(stock.seal_amount.astype(float))
    stock = stock.rename(columns={"trade_date": "signal_asof"})
    stock = stock.groupby(["signal_asof", "symbol"], as_index=False).agg({
        "event_limit_up": "max", "event_broken": "max",
        "event_limit_down": "max", "event_board_level": "max",
        "event_broken_count": "max", "event_log_seal_amount": "max",
    })
    return daily, stock


def attach_close_event_features(predictions, daily, stock, score_column):
    """Join only dates backed by complete batches and encode non-events as zero."""
    allowed = set(daily.signal_asof.astype(int))
    result = predictions[predictions.signal_asof.astype(int).isin(allowed)].copy()
    result = result.merge(daily, on="signal_asof", how="inner",
                          validate="many_to_one")
    result = result.merge(stock, on=["signal_asof", "symbol"], how="left",
                          validate="one_to_one")
    event_columns = [
        "event_limit_up", "event_broken", "event_limit_down",
        "event_board_level", "event_broken_count", "event_log_seal_amount",
    ]
    result[event_columns] = result[event_columns].fillna(0.0)
    rank = result.groupby("signal_asof")[score_column].rank(
        method="average", pct=True)
    result["base_rank_centered"] = rank - 0.5
    result["base_x_sentiment_z63"] = (
        result.base_rank_centered * result.sentiment_z63)
    result["base_x_risk_z63"] = result.base_rank_centered * result.risk_z63
    return result

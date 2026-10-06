# -*- encoding: utf-8 -*-
"""Lagged Dragon-Tiger List factors with explicit backfill provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


DATASET_VERSION = "akshare_eastmoney_lhb_backfill_v1"
LHB_FEATURES = (
    "lhb_event_1d",
    "lhb_net_buy_ratio_1d",
    "lhb_institution_present_1d",
    "lhb_institution_net_ratio_1d",
    "lhb_institution_count_balance_1d",
    "lhb_event_frequency_20d",
    "lhb_net_buy_pressure_20d",
)


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
    rows = []
    for month_root in sorted(Path(root).glob("batches/" + "[0-9]" * 6)):
        complete = []
        for path in sorted(month_root.glob("*/manifest.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") == "complete":
                complete.append((path, payload))
        if not complete:
            continue
        path, payload = complete[-1]
        if payload.get("dataset_version") != DATASET_VERSION:
            raise ValueError("LHB history dataset version mismatch")
        for key in ("raw", "detail", "institution"):
            artifact = Path(payload[key + "_path"])
            if _sha256(artifact) != payload[key + "_sha256"]:
                raise ValueError("LHB history hash mismatch: {}".format(path))
        if start_date is not None and int(payload["query_end"]) < int(start_date):
            continue
        if end_date is not None and int(payload["query_start"]) > int(end_date):
            continue
        rows.append(payload)
    return rows


def load_complete_history(root, start_date=None, end_date=None):
    manifests = complete_history_manifests(root, start_date, end_date)
    detail, institution, sessions = [], [], []
    for manifest in manifests:
        detail.append(pd.read_csv(
            manifest["detail_path"], dtype={"code": str}))
        institution.append(pd.read_csv(
            manifest["institution_path"], dtype={"code": str}))
        sessions.extend(int(value) for value in manifest["covered_sessions"])
    if not manifests:
        return pd.DataFrame(), pd.DataFrame(), [], manifests
    detail = pd.concat(detail, ignore_index=True) if detail else pd.DataFrame()
    institution = (pd.concat(institution, ignore_index=True)
                   if institution else pd.DataFrame())
    if len(detail):
        detail = detail[detail.trade_date.between(
            int(start_date or detail.trade_date.min()),
            int(end_date or detail.trade_date.max()))]
        detail["symbol"] = detail.code.map(_symbol)
    if len(institution):
        institution = institution[institution.trade_date.between(
            int(start_date or institution.trade_date.min()),
            int(end_date or institution.trade_date.max()))]
        institution["symbol"] = institution.code.map(_symbol)
    sessions = sorted(set(value for value in sessions
                          if (start_date is None or value >= int(start_date)) and
                          (end_date is None or value <= int(end_date))))
    return detail.reset_index(drop=True), institution.reset_index(
        drop=True), sessions, manifests


def _safe_ratio(numerator, denominator):
    left = np.asarray(numerator, dtype=float)
    right = np.asarray(denominator, dtype=float)
    output = np.zeros(np.broadcast(left, right).shape, dtype=float)
    np.divide(left, right, out=output,
              where=np.isfinite(right) & (right > 0))
    return np.clip(output, -1.0, 1.0)


def _select_detail(detail):
    if detail.empty:
        return pd.DataFrame(columns=[
            "source_date", "symbol", "lhb_event_1d",
            "lhb_net_buy_ratio_1d"])
    frame = detail.copy()
    numeric = ("net_buy", "buy_amount", "sell_amount", "lhb_turnover")
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.sort_values(
        ["trade_date", "symbol", "lhb_turnover"], kind="mergesort")
    # Multiple listing reasons can repeat identical totals or provide separate
    # day/three-day totals.  Select the largest reported transaction scope once
    # instead of double-counting reason rows.
    frame = frame.drop_duplicates(["trade_date", "symbol"], keep="last")
    denominator = frame.buy_amount + frame.sell_amount
    return pd.DataFrame({
        "source_date": frame.trade_date.astype(int),
        "symbol": frame.symbol.astype(str),
        "lhb_event_1d": 1.0,
        "lhb_net_buy_ratio_1d": _safe_ratio(frame.net_buy, denominator),
    })


def _select_institution(institution):
    if institution.empty:
        return pd.DataFrame(columns=[
            "source_date", "symbol", "lhb_institution_present_1d",
            "lhb_institution_net_ratio_1d",
            "lhb_institution_count_balance_1d"])
    frame = institution.copy()
    numeric = (
        "buyer_count", "seller_count", "institution_buy",
        "institution_sell", "institution_net_buy",
    )
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["institution_gross"] = (
        frame.institution_buy.fillna(0) + frame.institution_sell.fillna(0))
    frame = frame.sort_values(
        ["trade_date", "symbol", "institution_gross"], kind="mergesort")
    frame = frame.drop_duplicates(["trade_date", "symbol"], keep="last")
    count_total = frame.buyer_count.fillna(0) + frame.seller_count.fillna(0)
    return pd.DataFrame({
        "source_date": frame.trade_date.astype(int),
        "symbol": frame.symbol.astype(str),
        "lhb_institution_present_1d": 1.0,
        "lhb_institution_net_ratio_1d": _safe_ratio(
            frame.institution_net_buy, frame.institution_gross),
        "lhb_institution_count_balance_1d": _safe_ratio(
            frame.buyer_count.fillna(0) - frame.seller_count.fillna(0),
            count_total),
    })


def build_lhb_features(predictions, detail, institution, complete_sessions,
                       calendar_dates, rolling_sessions=20,
                       availability_lag_sessions=1, score_column="ridge_score"):
    """Attach only previously completed LHB sessions to prediction rows."""
    if int(availability_lag_sessions) < 1:
        raise ValueError("LHB features require at least one-session lag")
    sessions = sorted(set(int(value) for value in complete_sessions))
    calendar = [int(value) for value in calendar_dates]
    covered = set(sessions)
    date_position = {value: index for index, value in enumerate(calendar)}
    signal_dates = sorted(set(predictions.signal_asof.astype(int)))
    source_by_signal = {}
    for signal in signal_dates:
        position = date_position.get(signal)
        if position is None or position < int(availability_lag_sessions):
            continue
        source = calendar[position - int(availability_lag_sessions)]
        if source in covered:
            source_by_signal[signal] = source
    result = predictions[predictions.signal_asof.isin(source_by_signal)].copy()
    result["source_date"] = result.signal_asof.map(source_by_signal).astype(int)
    symbols = sorted(set(result.symbol.astype(str)))
    used_source_dates = sorted(set(source_by_signal.values()))
    first_position = date_position[used_source_dates[0]]
    warmup_start = max(0, first_position - int(rolling_sessions) + 1)
    last_position = date_position[used_source_dates[-1]]
    source_dates = [value for value in calendar[warmup_start:last_position + 1]
                    if value in covered]
    dense = pd.MultiIndex.from_product(
        [source_dates, symbols], names=["source_date", "symbol"]
    ).to_frame(index=False)
    one_day = _select_detail(detail)
    inst = _select_institution(institution)
    dense = dense.merge(one_day, on=["source_date", "symbol"], how="left",
                        validate="one_to_one")
    dense = dense.merge(inst, on=["source_date", "symbol"], how="left",
                        validate="one_to_one")
    one_day_columns = [
        "lhb_event_1d", "lhb_net_buy_ratio_1d",
        "lhb_institution_present_1d", "lhb_institution_net_ratio_1d",
        "lhb_institution_count_balance_1d",
    ]
    dense[one_day_columns] = dense[one_day_columns].fillna(0.0)
    dense = dense.sort_values(["symbol", "source_date"], kind="mergesort")
    grouped = dense.groupby("symbol", sort=False)
    dense["lhb_event_frequency_20d"] = grouped["lhb_event_1d"].transform(
        lambda value: value.rolling(
            int(rolling_sessions), min_periods=1).sum() / int(rolling_sessions))
    dense["lhb_net_buy_pressure_20d"] = grouped[
        "lhb_net_buy_ratio_1d"].transform(
            lambda value: value.rolling(
                int(rolling_sessions), min_periods=1).mean())
    result = result.merge(
        dense[["source_date", "symbol", *LHB_FEATURES]],
        on=["source_date", "symbol"], how="left", validate="many_to_one")
    if result[list(LHB_FEATURES)].isna().any().any():
        raise AssertionError("LHB dense feature construction left missing values")
    rank = result.groupby("signal_asof")[score_column].rank(
        method="average", pct=True)
    result["base_rank_centered"] = rank - .5
    result["lhb_availability_lag_sessions"] = int(
        availability_lag_sessions)
    result["availability_evidence"] = "BACKFILLED_QUERY"
    result["strict_pit_allowed"] = False
    return result

# -*- encoding: utf-8 -*-
"""Deterministic latency-aware paired replay for approved buy orders."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pandas as pd

from ..MarketBu.ABuRealtimeMarket import MinuteBarEvent, _as_shanghai_timestamp
from .ABuIntradayExecution import (
    IntradayExecutionConfig, instruction_from_order, simulate_intraday_order,
)
from .ABuTradeIntent import ApprovedOrder, Fill


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovedOrderSnapshot:
    order_id: str
    intent_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    quantity: int
    valid_session: int
    max_buy_price_raw: float
    initial_stop_raw: float | None
    snapshot_sha256: str

    @classmethod
    def from_order(cls, order):
        payload = {
            "order_id": order.order_id, "intent_id": order.intent_id,
            "strategy_id": order.strategy_id,
            "strategy_version": order.strategy_version,
            "symbol": order.symbol, "quantity": order.quantity,
            "valid_session": order.valid_session,
            "max_buy_price_raw": order.max_buy_price_raw,
            "initial_stop_raw": order.initial_stop_raw,
        }
        return cls(**payload, snapshot_sha256=_sha256(payload))


@dataclass(frozen=True)
class ReplayExecutionRecord:
    order_id: str
    approved_order_sha256: str
    symbol: str
    policy_id: str
    state: str
    reason_code: str
    fill_price_raw: float
    d0_fill_price_raw: float
    price_delta_bps_vs_d0: float | None
    candidate_bar_start: str = ""
    capacity_reference_bar_end: str = ""


class LatencyModel(object):
    model_id = "abstract"

    def delay_ms(self, event):
        raise NotImplementedError

    def apply(self, event):
        available = _as_shanghai_timestamp(event.bar_end) + pd.Timedelta(
            milliseconds=self.delay_ms(event))
        timestamp = available.isoformat()
        return replace(
            event, request_started_at=timestamp, received_at=timestamp,
            available_at=timestamp)

    def to_dict(self):
        return {"model_id": self.model_id}


class ZeroDelayModel(LatencyModel):
    model_id = "ideal_zero_delay"

    def delay_ms(self, event):
        return 0


class FixedDelayModel(LatencyModel):
    model_id = "fixed_delay"

    def __init__(self, milliseconds):
        if milliseconds < 0:
            raise ValueError("milliseconds must be non-negative")
        self.milliseconds = int(milliseconds)

    def delay_ms(self, event):
        return self.milliseconds

    def to_dict(self):
        return {"model_id": self.model_id,
                "milliseconds": self.milliseconds}


class EmpiricalDelayModel(LatencyModel):
    model_id = "empirical_delay"

    def __init__(self, samples_ms, sample_manifest_sha256, seed=0):
        samples = tuple(int(value) for value in samples_ms)
        if not samples or any(value < 0 for value in samples):
            raise ValueError("empirical delay samples must be non-empty/non-negative")
        if not sample_manifest_sha256:
            raise ValueError("empirical model requires a sample manifest")
        self.samples_ms = samples
        self.sample_manifest_sha256 = str(sample_manifest_sha256)
        self.seed = int(seed)

    def delay_ms(self, event):
        key = "{}|{}|{}|{}|{}".format(
            self.seed, event.symbol, event.bar_end, event.source,
            event.revision).encode("utf-8")
        index = int(hashlib.sha256(key).hexdigest()[:16], 16) % len(
            self.samples_ms)
        return self.samples_ms[index]

    def to_dict(self):
        return {
            "model_id": self.model_id, "samples_ms": list(self.samples_ms),
            "sample_manifest_sha256": self.sample_manifest_sha256,
            "seed": self.seed,
        }


class MinuteReplayClock(object):
    """Yield events in deterministic availability order after latency mapping."""

    def __init__(self, events, latency_model):
        self.events = tuple(events)
        self.latency_model = latency_model

    def __iter__(self):
        adjusted = [self.latency_model.apply(item) for item in self.events]
        return iter(sorted(adjusted, key=lambda item: (
            _as_shanghai_timestamp(item.available_at),
            _as_shanghai_timestamp(item.bar_end), item.source, item.revision)))


def _d0_values(value):
    if isinstance(value, Fill):
        return value.status, value.reason_code, float(value.fill_price_raw)
    if value is None:
        return "missing", "D0_RESULT_MISSING", 0.0
    return (str(value.get("status", "missing")),
            str(value.get("reason_code", "")),
            float(value.get("fill_price_raw", 0.0)))


def run_paired_replay(orders, bars_by_symbol, d0_by_order,
                      execution_config=None, latency_model=None,
                      upper_limits=None, source_snapshot_hash="",
                      data_manifest_hash=""):
    """Replay fixed orders; policy outcomes never alter another order."""
    execution_config = execution_config or IntradayExecutionConfig()
    latency_model = latency_model or ZeroDelayModel()
    upper_limits = upper_limits or {}
    records = []
    snapshots = []
    for order in sorted(orders, key=lambda item: item.order_id):
        if order.side != "buy":
            continue
        snapshot = ApprovedOrderSnapshot.from_order(order)
        snapshots.append(snapshot)
        d0_status, d0_reason, d0_price = _d0_values(
            d0_by_order.get(order.order_id))
        records.append(ReplayExecutionRecord(
            order_id=order.order_id,
            approved_order_sha256=snapshot.snapshot_sha256,
            symbol=order.symbol, policy_id="D0",
            state=("FILLED" if d0_status == "filled" else "EXPIRED"),
            reason_code=d0_reason, fill_price_raw=d0_price,
            d0_fill_price_raw=d0_price,
            price_delta_bps_vs_d0=0.0 if d0_price > 0 else None,
        ))
        bars = tuple(MinuteReplayClock(
            bars_by_symbol.get(order.symbol, ()), latency_model))
        for policy in ("M1", "M2"):
            instruction = instruction_from_order(
                order, order.valid_session, policy, execution_config)
            outcome = simulate_intraday_order(
                instruction, order, bars, config=execution_config,
                upper_limit_raw=upper_limits.get(order.symbol))
            delta = ((outcome.fill_price_raw / d0_price - 1) * 10000.0
                     if outcome.state == "FILLED" and d0_price > 0 else None)
            records.append(ReplayExecutionRecord(
                order_id=order.order_id,
                approved_order_sha256=snapshot.snapshot_sha256,
                symbol=order.symbol, policy_id=policy,
                state=outcome.state, reason_code=outcome.reason_code,
                fill_price_raw=outcome.fill_price_raw,
                d0_fill_price_raw=d0_price,
                price_delta_bps_vs_d0=delta,
                candidate_bar_start=outcome.candidate_bar_start,
                capacity_reference_bar_end=(
                    outcome.capacity_reference_bar_end),
            ))
    manifest_body = {
        "schema_version": "paired_minute_replay_v1",
        "source_snapshot_hash": source_snapshot_hash,
        "data_manifest_hash": data_manifest_hash,
        "latency_model": latency_model.to_dict(),
        "execution_config": asdict(execution_config),
        "approved_orders_sha256": _sha256([
            asdict(item) for item in snapshots]),
        "records_sha256": _sha256([asdict(item) for item in records]),
    }
    manifest = dict(manifest_body)
    manifest["manifest_sha256"] = _sha256(manifest_body)
    return tuple(records), manifest


def write_paired_replay(directory, records, manifest):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    records_path = target / "paired_execution.jsonl"
    manifest_path = target / "run_manifest.json"
    if records_path.exists() or manifest_path.exists():
        raise FileExistsError("refusing to overwrite paired replay")
    records_text = "".join(
        _canonical_json(asdict(item)) + "\n" for item in records)
    records_path.write_text(records_text, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    return records_path, manifest_path

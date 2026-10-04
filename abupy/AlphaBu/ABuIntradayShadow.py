# -*- encoding: utf-8 -*-
"""Persistent broker-free minute execution shadow runner and quality gates."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..MarketBu.ABuMinuteBarStore import (
    MinuteBarStore, minute_events_from_frame,
)
from ..MarketBu.ABuRealtimeMarket import RealtimeMarketDataError
from .ABuIntradayExecution import (
    IntradayExecutionConfig, TERMINAL_STATES, instruction_from_order,
    simulate_intraday_order,
)
from .ABuTradeIntent import ApprovedOrder


SHADOW_STATE_VERSION = "intraday_shadow_state_v1"


def _canonical_json(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(payload):
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def initialize_shadow_state(directory, orders, policy_id,
                            source_snapshot_hash, config=None,
                            upper_limits=None, created_at=None):
    if policy_id not in ("M1", "M2"):
        raise ValueError("shadow policy must be M1 or M2")
    if not source_snapshot_hash:
        raise ValueError("shadow state requires source_snapshot_hash")
    directory = Path(directory)
    state_path = directory / "state.json"
    if state_path.exists():
        raise FileExistsError("shadow state already exists")
    config = config or IntradayExecutionConfig()
    upper_limits = upper_limits or {}
    frozen = []
    for order in sorted(orders, key=lambda item: item.order_id):
        if order.side != "buy":
            continue
        instruction = instruction_from_order(
            order, order.valid_session, policy_id, config,
            created_at=created_at or "")
        frozen.append({
            "order": asdict(order), "instruction": asdict(instruction),
            "upper_limit_raw": upper_limits.get(order.symbol),
        })
    state = {
        "state_version": SHADOW_STATE_VERSION,
        "research_only": True,
        "broker_connected": False,
        "source_snapshot_hash": source_snapshot_hash,
        "policy_id": policy_id,
        "config": asdict(config),
        "created_at": created_at,
        "instructions": frozen,
        "order_events": [],
        "outcomes": {},
        "polls": [],
    }
    state["frozen_input_sha256"] = _sha256({
        "policy_id": policy_id, "config": state["config"],
        "instructions": frozen,
        "source_snapshot_hash": source_snapshot_hash,
    })
    _atomic_json(state_path, state)
    return state


def read_shadow_state(directory):
    state = json.loads((Path(directory) / "state.json").read_text(
        encoding="utf-8"))
    if state.get("state_version") != SHADOW_STATE_VERSION:
        raise ValueError("shadow state version mismatch")
    if state.get("broker_connected") is not False or \
            state.get("research_only") is not True:
        raise ValueError("shadow state safety flags changed")
    frozen = _sha256({
        "policy_id": state["policy_id"], "config": state["config"],
        "instructions": state["instructions"],
        "source_snapshot_hash": state["source_snapshot_hash"],
    })
    if frozen != state.get("frozen_input_sha256"):
        raise ValueError("shadow frozen inputs changed")
    return state


def _order(payload):
    item = dict(payload)
    item["limit_reason_codes"] = tuple(item.get("limit_reason_codes", ())) \
        if "limit_reason_codes" in item else item.get("limit_reason_codes")
    item.pop("limit_reason_codes", None)
    return ApprovedOrder(**item)


def _session_end(instruction, config):
    date = str(instruction["trading_date"])
    candidate = pd.Timestamp("{}-{}-{}T{}+08:00".format(
        date[:4], date[4:6], date[6:], config.last_candidate_start))
    return candidate + pd.Timedelta(minutes=1)


class IntradayShadowRunner(object):
    """Poll, archive, replay and persist hypothetical outcomes only."""

    def __init__(self, state_directory, adapter, minute_store, now=None):
        self.state_directory = Path(state_directory)
        self.adapter = adapter
        self.minute_store = (minute_store if isinstance(minute_store, MinuteBarStore)
                             else MinuteBarStore(minute_store))
        if hasattr(self.adapter, "raw_archive") and \
                self.adapter.raw_archive is None:
            self.adapter.raw_archive = self.minute_store.append_raw_response
        self._now = now or (lambda: pd.Timestamp.now(tz="Asia/Shanghai"))

    def poll_once(self, prefetched_health=None):
        """Advance decisions using either one shared fetch or local fetching.

        ``prefetched_health`` lets a session collector request every symbol once
        and feed the same immutable bar snapshot to M1 and M2.  ``None`` keeps
        the original standalone behaviour for callers that do not run a shared
        collector.
        """
        state = read_shadow_state(self.state_directory)
        config = IntradayExecutionConfig(**state["config"])
        now = pd.Timestamp(self._now())
        if now.tzinfo is None:
            raise ValueError("shadow clock must be timezone-aware")
        now = now.tz_convert("Asia/Shanghai")
        health_rows = []
        new_event_ids = {item["event_id"] for item in state["order_events"]}
        for frozen in state["instructions"]:
            order = _order(frozen["order"])
            if order.order_id in state["outcomes"] and \
                    state["outcomes"][order.order_id]["state"] in TERMINAL_STATES:
                continue
            if prefetched_health is None:
                date = str(order.valid_session)
                day = "{}-{}-{}".format(date[:4], date[4:6], date[6:])
                try:
                    frame = self.adapter.minute_bars(
                        order.symbol, period="1",
                        start=day + " 09:30:00",
                        end=now.strftime("%Y-%m-%d %H:%M:%S"), adjust="")
                    events = minute_events_from_frame(frame)
                    self.minute_store.append(events)
                    health_rows.append({
                        "symbol": order.symbol, "status": "OK",
                        "health": self.adapter.health().to_dict(),
                    })
                except RealtimeMarketDataError as error:
                    health_rows.append({
                        "symbol": order.symbol, "status": "ERROR",
                        "error": str(error),
                        "health": self.adapter.health().to_dict(),
                    })
            else:
                health_rows.append(prefetched_health.get(order.symbol, {
                    "symbol": order.symbol,
                    "status": "ERROR",
                    "error": "shared collector returned no health record",
                    "health": None,
                }))
            bars = self.minute_store.read(
                order.symbol, order.valid_session, 1, as_of=now)
            instruction_payload = frozen["instruction"]
            from .ABuTradeIntent import IntradayExecutionInstruction
            instruction = IntradayExecutionInstruction(**instruction_payload)
            should_finalize = now >= _session_end(instruction_payload, config)
            outcome = simulate_intraday_order(
                instruction, order, bars, config=config,
                upper_limit_raw=frozen.get("upper_limit_raw"),
                finalize=should_finalize)
            for event in outcome.events:
                if event.event_id not in new_event_ids:
                    state["order_events"].append(asdict(event))
                    new_event_ids.add(event.event_id)
            state["outcomes"][order.order_id] = asdict(outcome)
        state["polls"].append({
            "polled_at": now.isoformat(), "health": health_rows,
            "outcome_sha256": _sha256(state["outcomes"]),
        })
        _atomic_json(self.state_directory / "state.json", state)
        return state


@dataclass(frozen=True)
class ShadowQualityThresholds:
    min_window_coverage: float
    min_fresh_bar_availability: float
    max_p95_latency_ms: float
    max_p99_latency_ms: float
    max_stale_rate: float
    max_fallback_rate: float
    max_duplicate_rate: float
    min_recovery_rate: float

    def __post_init__(self):
        rates = (
            self.min_window_coverage, self.min_fresh_bar_availability,
            self.max_stale_rate, self.max_fallback_rate,
            self.max_duplicate_rate, self.min_recovery_rate,
        )
        if any(value < 0 or value > 1 for value in rates):
            raise ValueError("quality rates must be in [0, 1]")
        if self.max_p95_latency_ms < 0 or self.max_p99_latency_ms < 0:
            raise ValueError("latency thresholds must be non-negative")


def evaluate_shadow_quality(daily_reports, thresholds, minimum_days=20):
    """Evaluate frozen thresholds; calendar duration alone never passes."""
    reports = list(daily_reports)
    effective = []
    for report in reports:
        passed = (
            report["window_coverage"] >= thresholds.min_window_coverage and
            report["fresh_bar_availability"] >=
            thresholds.min_fresh_bar_availability and
            report["p95_latency_ms"] <= thresholds.max_p95_latency_ms and
            report["p99_latency_ms"] <= thresholds.max_p99_latency_ms and
            report["stale_rate"] <= thresholds.max_stale_rate and
            report["fallback_rate"] <= thresholds.max_fallback_rate and
            report["duplicate_rate"] <= thresholds.max_duplicate_rate and
            report["recovery_rate"] >= thresholds.min_recovery_rate and
            bool(report.get("audit_complete")) and
            bool(report.get("all_symbols_have_health"))
        )
        if passed:
            effective.append(report["trade_date"])
    return {
        "schema_version": "intraday_shadow_gate_v1",
        "observed_days": len(reports),
        "effective_days": len(effective),
        "minimum_days": int(minimum_days),
        "passed": len(reports) >= minimum_days and len(effective) == len(reports),
        "effective_trade_dates": effective,
        "thresholds_sha256": _sha256(asdict(thresholds)),
    }

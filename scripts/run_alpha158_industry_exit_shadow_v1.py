#!/usr/bin/env python3
"""Append-only forward shadow for the frozen I1 industry exit signal.

The observer reads the hash-verified Alpha158 paper account and its archived
panels.  It never mutates that account, creates orders, or changes positions.
Only sessions committed after activation count as prospective evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


PROJECT = Path("/Users/wjy/Documents/code/abu")
FORWARD = Path("/Users/wjy/abu/shadow/alpha158_forward_v1")
SHADOW = Path("/Users/wjy/abu/shadow/alpha158_industry_absolute_exit_i1_forward_v1")
ACCOUNT = "event_exit_only"
MIN_FORWARD_TRIGGER_DATES = 10
PARTIAL_EXIT_FRACTION = 0.50


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, np.generic):
        return value.item()
    return value


def _load_forward_module(forward):
    runtime = Path(forward) / "runtime"
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    from abupy.AlphaBu import ABuAlphaForwardShadow as module
    expected = runtime / "abupy/AlphaBu/ABuAlphaForwardShadow.py"
    if Path(module.__file__).resolve() != expected.resolve():
        raise ValueError("forward reader did not load the frozen runtime")
    return module


def _load_i1_module(root):
    path = Path(root) / "runtime/ABuMarketIndustryExit.py"
    name = "abupy.AlphaBu.ABuMarketIndustryExitI1Shadow"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load frozen I1 runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify(root):
    root = Path(root)
    registration = json.loads((root / "registration.json").read_text())
    for relative, digest in registration["runtime_hashes"].items():
        if sha256(root / relative) != digest:
            raise ValueError("I1 shadow runtime integrity failure: " + relative)
    forward = Path(registration["forward_root"])
    if sha256(forward / "genesis/commit.json") != registration["forward_genesis_sha256"]:
        raise ValueError("registered forward account was replaced")
    if (not registration["research_only"] or
            registration["account_mutation_allowed"] or
            registration["order_creation_allowed"]):
        raise ValueError("I1 shadow isolation contract changed")
    return registration, forward


def _pending_base_sells(account):
    return {
        order.symbol: getattr(order, "reason", "")
        for order in account["executor"].orders
        if order.side == "sell"
    }


def build_observations(panel, state, context, trade_date):
    """Return non-mutating I1 observations for current paper holdings."""
    account = state["accounts"][ACCOUNT]
    executor = account["executor"]
    base_sells = _pending_base_sells(account)
    day = len(panel.dates) - 1
    if int(panel.dates[day]) != int(trade_date):
        raise ValueError("panel/account trade date mismatch")
    rows = []
    for symbol, position in sorted(executor.positions.items()):
        exit_state = account["exit_states"].get(symbol)
        column = panel.symbol_index[symbol]
        industry = int(panel.industry[day, column])
        reason, observed_industry = context.industry_signal(day, symbol)
        if observed_industry != industry:
            raise AssertionError("industry state lookup mismatch")
        trailing = bool(exit_state and exit_state.trailing_enabled)
        triggered = bool(reason and trailing)
        base_reason = str(base_sells.get(symbol, ""))
        incremental = bool(triggered and not base_reason)
        feature = context.industry_features

        def value(name):
            if industry < 0 or industry >= feature[name].shape[1]:
                return None
            item = float(feature[name][day, industry])
            return item if np.isfinite(item) else None

        rows.append({
            "trade_date": int(trade_date),
            "symbol": symbol,
            "quantity": int(position.quantity),
            "industry_id": industry,
            "trailing_enabled": trailing,
            "i1_reason": str(reason or ""),
            "i1_triggered": triggered,
            "base_sell_pending": bool(base_reason),
            "base_sell_reason": base_reason,
            "incremental_shadow_exit": incremental,
            "return_5d": value("return_5d"),
            "intraday_return": value("intraday_return"),
            "amount_ratio_20": value("amount_ratio_20"),
            "advance_ratio": value("advance_ratio"),
            "return_hot_threshold": value("return_hot_threshold"),
            "intraday_reversal_threshold": value(
                "intraday_reversal_threshold"),
            "return_retreat_threshold": value("return_retreat_threshold"),
            "advance_retreat_threshold": value(
                "advance_retreat_threshold"),
            "amount_hot_threshold": value("amount_hot_threshold"),
            "amount_retreat_threshold": value(
                "amount_retreat_threshold"),
        })
    return rows


def _session_payload(root, forward_path, panel, state, context, registration):
    date = int(state["last_processed_date"])
    rows = build_observations(panel, state, context, date)
    triggers = [row for row in rows if row["incremental_shadow_exit"]]
    market_reason = context.market_signal(len(panel.dates) - 1) or ""
    return {
        "schema_version": "alpha158_i1_industry_exit_shadow_session_v1",
        "trade_date": date,
        "forward_session": int(state["sessions"]),
        "forward_commit": sha256(forward_path / "commit.json"),
        "historical_cutoff": int(registration["historical_cutoff"]),
        "market_context_reason": market_reason,
        "held_positions": len(rows),
        "i1_trigger_count": len(triggers),
        "i1_trigger_symbols": [row["symbol"] for row in triggers],
        "observations": rows,
        "research_only": True,
        "account_mutated": False,
        "orders_created": 0,
    }


def _write_session(root, payload):
    directory = Path(root) / "sessions" / str(payload["trade_date"])
    path = directory / "snapshot.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if saved != payload:
            raise ValueError("immutable I1 shadow session conflict")
        return False
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(path, payload)
    observations = pd.DataFrame(payload["observations"])
    observations.to_csv(directory / "observations.csv", index=False)
    return True


def summary(root, registration, forward_sessions=None):
    sessions = []
    for path in sorted((Path(root) / "sessions").glob("*/snapshot.json")):
        sessions.append(json.loads(path.read_text()))
    trigger_dates = [
        int(item["trade_date"]) for item in sessions
        if int(item["i1_trigger_count"]) > 0
    ]
    result = {
        "schema_version": "alpha158_i1_industry_exit_shadow_summary_v1",
        "status": ("PARTIAL_EXIT_VALIDATION_ELIGIBLE"
                   if len(trigger_dates) >= registration[
                       "minimum_forward_trigger_dates"] else
                   "COLLECTING_FORWARD_EVENTS"),
        "historical_cutoff": int(registration["historical_cutoff"]),
        "processed_forward_sessions": len(sessions),
        "latest_trade_date": (int(sessions[-1]["trade_date"])
                              if sessions else None),
        "forward_independent_trigger_dates": len(trigger_dates),
        "trigger_dates": trigger_dates,
        "minimum_forward_trigger_dates": int(
            registration["minimum_forward_trigger_dates"]),
        "remaining_trigger_dates": max(
            0, int(registration["minimum_forward_trigger_dates"]) -
            len(trigger_dates)),
        "partial_exit_fraction_reserved": float(
            registration["partial_exit_fraction_reserved"]),
        "partial_exit_test_locked": len(trigger_dates) < registration[
            "minimum_forward_trigger_dates"],
        "forward_sessions_available": forward_sessions,
        "research_only": True,
        "account_mutation_allowed": False,
        "order_creation_allowed": False,
    }
    atomic_json(Path(root) / "summary.json", result)
    return result


def activate(root, forward):
    root, forward = Path(root), Path(forward)
    if root.exists():
        registration, _ = verify(root)
        return {"status": "ALREADY_ACTIVATED", "summary": summary(
            root, registration)}
    forward_module = _load_forward_module(forward)
    paths, _ = forward_module.verify_chain(forward)
    state = forward_module.load_pickle(paths[-1] / "state.pkl.gz")
    root.mkdir(parents=True)
    runtime = root / "runtime"
    runtime.mkdir()
    files = {
        "runtime/run_alpha158_industry_exit_shadow_v1.py": Path(__file__),
        "runtime/ABuMarketIndustryExit.py": (
            PROJECT / "abupy/AlphaBu/ABuMarketIndustryExit.py"),
        "runtime/alpha158_market_industry_exit_v1.json": (
            PROJECT / "configs/selection/alpha158_market_industry_exit_v1.json"),
    }
    for relative, source in files.items():
        shutil.copy2(source, root / relative)
    registration = {
        "schema_version": "alpha158_i1_industry_exit_shadow_registration_v1",
        "activated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "forward_root": str(forward),
        "forward_genesis_sha256": sha256(forward / "genesis/commit.json"),
        "historical_cutoff": int(state["historical_cutoff"]),
        "activation_last_processed_date": int(state["last_processed_date"]),
        "activation_forward_sessions": int(state["sessions"]),
        "variant": "I1_industry",
        "minimum_forward_trigger_dates": MIN_FORWARD_TRIGGER_DATES,
        "partial_exit_fraction_reserved": PARTIAL_EXIT_FRACTION,
        "partial_exit_test_locked_until_gate": True,
        "historical_trigger_dates_excluded": True,
        "research_only": True,
        "account_mutation_allowed": False,
        "order_creation_allowed": False,
    }
    registration["runtime_hashes"] = {
        relative: sha256(root / relative) for relative in files
    }
    atomic_json(root / "registration.json", registration)
    (root / "sessions").mkdir()
    return {"status": "ACTIVATED", "summary": summary(root, registration)}


def run(root):
    root = Path(root)
    registration, forward = verify(root)
    forward_module = _load_forward_module(forward)
    paths, _ = forward_module.verify_chain(forward)
    base_sessions = int(registration["activation_forward_sessions"])
    available = len(paths) - 1
    new_paths = paths[base_sessions + 1:]
    i1 = _load_i1_module(root)
    config = i1.load_market_industry_exit_config(
        root / "runtime/alpha158_market_industry_exit_v1.json", "industry")
    created = 0
    for index, path in enumerate(new_paths, start=base_sessions + 1):
        state = forward_module.load_pickle(path / "state.pkl.gz")
        if int(state["sessions"]) != index:
            raise ValueError("forward session sequence mismatch")
        panel = forward_module.last_panel(paths[:index + 1])
        context = i1.MarketIndustryAbsoluteState(panel, config)
        payload = _session_payload(
            root, path, panel, state, context, registration)
        created += int(_write_session(root, payload))
    result = summary(root, registration, forward_sessions=available)
    result["new_sessions_recorded"] = created
    result["status"] = ("WAITING_FOR_FIRST_FORWARD_SESSION"
                        if available == base_sessions else result["status"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("activate", "run", "status"))
    parser.add_argument("--root", type=Path, default=SHADOW)
    parser.add_argument("--forward-root", type=Path, default=FORWARD)
    args = parser.parse_args()
    if args.command == "activate":
        value = activate(args.root.resolve(), args.forward_root.resolve())
    else:
        frozen = args.root.resolve() / "runtime/run_alpha158_industry_exit_shadow_v1.py"
        if Path(__file__).resolve() != frozen.resolve():
            os.execv(sys.executable, [
                sys.executable, "-B", str(frozen), args.command,
                "--root", str(args.root.resolve()),
            ])
        with (args.root.resolve() / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            registration, _ = verify(args.root.resolve())
            value = (run(args.root.resolve()) if args.command == "run" else
                     summary(args.root.resolve(), registration))
    print(json.dumps(value, ensure_ascii=False, indent=2,
                     sort_keys=True, allow_nan=False, default=_plain))


if __name__ == "__main__":
    main()

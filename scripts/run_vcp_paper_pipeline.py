#!/usr/bin/env python3
"""Update the market snapshot and advance the VCP paper account once."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def run(command):
    completed = subprocess.run(
        command, cwd=str(ROOT), text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(
            "command failed ({}):\n{}\n{}".format(
                completed.returncode, completed.stdout, completed.stderr))
    return completed.stdout


def run_shadow(command):
    """Run a diagnostic sidecar without blocking the frozen paper strategy."""
    completed = subprocess.run(
        command, cwd=str(ROOT), text=True, capture_output=True)
    if completed.returncode:
        return {
            "status": "shadow_error", "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:],
        }
    try:
        return json.loads(completed.stdout)
    except ValueError:
        return {"status": "shadow_output_invalid", "stdout": completed.stdout[-2000:]}


def enforce_shortline_shadow_contract(result):
    """Reject any collector result that claims permission to affect orders."""
    result = dict(result or {})
    status = str(result.get("status", ""))
    if status.startswith("skipped_") or status.startswith("shadow_"):
        result["shadow_contract_status"] = "not_applicable"
        return result
    if (result.get("feature_mode") != "shadow_only" or
            result.get("order_mutation_allowed") is not False or
            result.get("paper_order_effect") != "none"):
        return {
            "status": "shadow_contract_rejected",
            "reason": "short-line capture is not provably isolated from paper orders",
            "collector_status": status,
        }
    result["shadow_contract_status"] = "passed"
    return result


def shortline_trade_date(market_update, now=None):
    """Return today's completed session, including an idempotent late rerun."""
    if not market_update:
        return None
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    today = int(now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"))
    if market_update.get("status") == "updated":
        return int(market_update["trade_date"])
    if (market_update.get("status") == "no_new_session" and
            market_update.get("cached_date") is not None and
            int(market_update["cached_date"]) == today):
        return today
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-dir", type=Path,
                        default=Path("/Users/wjy/abu/paper/vcp_residual_v2"))
    parser.add_argument("--skip-update", action="store_true")
    parser.add_argument("--skip-shortline", action="store_true")
    parser.add_argument("--shortline-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/shortline_forward"))
    args = parser.parse_args()
    python = str(ROOT / ".venv/bin/python")
    outputs = {}
    if not args.skip_update:
        outputs["market_update"] = run([
            python, "scripts/update_paper_market_data.py",
            "--paper-dir", str(args.paper_dir),
        ])
    market_update = (json.loads(outputs["market_update"])
                     if "market_update" in outputs else None)
    event_trade_date = shortline_trade_date(market_update)
    if args.skip_shortline:
        shortline = {"status": "skipped_by_cli"}
    elif event_trade_date is not None:
        shortline = enforce_shortline_shadow_contract(run_shadow([
            python, "scripts/collect_shortline_events.py",
            "--trade-date", str(event_trade_date),
            "--phase", "close", "--output-dir", str(args.shortline_dir),
            "--paper-dir", str(args.paper_dir),
        ]))
    else:
        shortline = {
            "status": "skipped_without_new_market_session",
            "market_update_status": (market_update or {}).get("status", "not_run"),
        }
    outputs["paper_run"] = run([
        python, "scripts/run_vcp_paper_daily.py",
        "--paper-dir", str(args.paper_dir),
    ])
    outputs["wecom"] = run([
        python, "scripts/notify_wecom_paper_trades.py",
        "--paper-dir", str(args.paper_dir),
    ])
    last_run = json.loads((args.paper_dir / "last_run.json").read_text())
    wecom = json.loads(outputs["wecom"])
    result = {
        "status": "ok", "paper_dir": str(args.paper_dir),
        "event": last_run, "shortline_shadow": shortline, "wecom": wecom,
        "shortline_order_integration": "disabled",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

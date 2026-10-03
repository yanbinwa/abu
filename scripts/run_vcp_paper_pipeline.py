#!/usr/bin/env python3
"""Update the market snapshot and advance the VCP paper account once."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command):
    completed = subprocess.run(
        command, cwd=str(ROOT), text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(
            "command failed ({}):\n{}\n{}".format(
                completed.returncode, completed.stdout, completed.stderr))
    return completed.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-dir", type=Path,
                        default=Path("/Users/wjy/abu/paper/vcp_residual_v2"))
    parser.add_argument("--skip-update", action="store_true")
    args = parser.parse_args()
    python = str(ROOT / ".venv/bin/python")
    outputs = {}
    if not args.skip_update:
        outputs["market_update"] = run([
            python, "scripts/update_paper_market_data.py",
            "--paper-dir", str(args.paper_dir),
        ])
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
        "event": last_run, "wecom": wecom,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

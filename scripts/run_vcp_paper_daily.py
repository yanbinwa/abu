#!/usr/bin/env python3
"""Initialize or advance the persistent VCP paper-trading account."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuVCPPaperTrading import (  # noqa: E402
    initialize_paper, run_paper_sessions,
)
from abupy.AlphaBu.ABuVCPStrategy import (  # noqa: E402
    load_vcp_attention_config, load_vcp_core_config, load_vcp_residual_config,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--paper-dir", type=Path,
                        default=Path("/Users/wjy/abu/paper/vcp_residual_v2"))
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    args.paper_dir.mkdir(parents=True, exist_ok=True)

    core = load_vcp_core_config(ROOT / "configs/selection/vcp_core_v1.json")
    attention = load_vcp_attention_config(
        ROOT / "configs/selection/vcp_attention_v1.json")
    residual = load_vcp_residual_config(
        ROOT / "configs/selection/vcp_residual_v2.json")
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=20991231)
    state_path = args.paper_dir / "state.json"
    if args.initialize:
        state, summary = initialize_paper(
            panel, args.paper_dir, core, attention, residual, risk, ROOT)
        event = {"status": "initialized", "summary": summary,
                 "pending_orders": len(state["active"]["orders"])}
    else:
        if not state_path.exists():
            raise SystemExit("paper account is not initialized; run with --initialize")
        _, summary, run = run_paper_sessions(
            panel, args.paper_dir, core, attention, residual, risk)
        event = {"status": run["status"], "run": run, "summary": summary}
    (args.paper_dir / "last_run.json").write_text(
        json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(event, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

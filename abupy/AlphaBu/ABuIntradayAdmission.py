# -*- encoding: utf-8 -*-
"""Evidence-based admission decisions for minute execution v1."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


def _hash(payload):
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AdmissionEvidence:
    software_milestones_passed: bool
    full_regression_passed: bool
    d0_parity_passed: bool
    causal_tests_passed: bool
    paired_replay_completed: bool
    portfolio_backtest_completed: bool
    calibration_effective_days: int
    thresholds_frozen: bool
    qualification_effective_days: int
    shadow_quality_passed: bool
    unresolved_critical_defects: int = 0

    def __post_init__(self):
        if min(self.calibration_effective_days,
               self.qualification_effective_days,
               self.unresolved_critical_defects) < 0:
            raise ValueError("admission counts must be non-negative")


def evaluate_admission(evidence):
    body = asdict(evidence)
    software = all((
        evidence.software_milestones_passed,
        evidence.full_regression_passed,
        evidence.d0_parity_passed,
        evidence.causal_tests_passed,
        evidence.paired_replay_completed,
        evidence.portfolio_backtest_completed,
    ))
    reasons = []
    if evidence.unresolved_critical_defects:
        decision = "REJECT"
        reasons.append("UNRESOLVED_CRITICAL_DEFECTS")
    elif not software:
        decision = "REJECT"
        reasons.append("SOFTWARE_EVIDENCE_INCOMPLETE")
    elif evidence.calibration_effective_days < 10 or \
            not evidence.thresholds_frozen:
        decision = "ACCEPT_DATA_ONLY"
        reasons.append("CALIBRATION_GATE_PENDING")
    elif evidence.qualification_effective_days < 20:
        decision = "ACCEPT_DATA_ONLY"
        reasons.append("QUALIFICATION_WINDOW_PENDING")
    elif not evidence.shadow_quality_passed:
        decision = "REVISE_AND_REPEAT"
        reasons.append("SHADOW_QUALITY_GATE_FAILED")
    else:
        decision = "ACCEPT_V1"
        reasons.append("ALL_GATES_PASSED")
    report = {
        "schema_version": "intraday_admission_v1",
        "decision": decision,
        "reason_codes": reasons,
        "evidence": body,
        "minute_sell_research_allowed": decision == "ACCEPT_V1",
    }
    report["report_sha256"] = _hash(report)
    return report


def write_admission_report(path, report):
    target = Path(path)
    if target.exists():
        raise FileExistsError("refusing to overwrite admission report")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    return target

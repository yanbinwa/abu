# -*- encoding: utf-8 -*-
"""Mechanical historical-screen decision for registered ML experiments."""
from __future__ import annotations

from .ABuMLContracts import ExperimentDecision, MLContractError, sha256_payload


FINAL_STATES = {
    "REJECT_BASELINE_NOT_REPRODUCED",
    "REJECT_DATA_OR_LEAKAGE_AUDIT",
    "REJECT_HISTORICAL_SCREEN",
    "INCONCLUSIVE_INCOMPLETE_COVERAGE",
    "INCONCLUSIVE_HISTORICAL_SCREEN",
    "PASS_HISTORICAL_SCREEN",
}


def _finite(value):
    try:
        return value is not None and float(value) == float(value)
    except (TypeError, ValueError):
        return False


def decide_historical_screen(registration, candidate_arm_id, evidence,
                             minimum_annualized_uplift_pp=0.5,
                             top5_share_max=0.5):
    """Evaluate only pre-registered gates; diagnostic blocks never decide."""
    if candidate_arm_id not in registration.candidate_arm_ids:
        raise MLContractError("candidate arm is not registered")
    if evidence.get("baseline_arm_id") != registration.baseline_arm_id:
        raise MLContractError("evidence baseline does not match registration")
    if evidence.get("candidate_arm_id") != candidate_arm_id:
        raise MLContractError("evidence candidate does not match registration")

    if not evidence.get("baseline_reproduced", False):
        state = "REJECT_BASELINE_NOT_REPRODUCED"
    elif not evidence.get("data_and_leakage_audit_passed", False):
        state = "REJECT_DATA_OR_LEAKAGE_AUDIT"
    elif not evidence.get("coverage_complete", False):
        state = "INCONCLUSIVE_INCOMPLETE_COVERAGE"
    else:
        costs = evidence.get("cost_scenarios", {})
        primary = costs.get("25bp", {})
        decisive = [
            primary.get("annualized_uplift_pp"),
            primary.get("calmar_candidate"),
            primary.get("calmar_baseline"),
            evidence.get("bootstrap_20", {}).get(
                "annualized_return", {}).get("lower"),
            evidence.get("top5_positive_profit_share"),
            evidence.get("positive_year_count"),
        ]
        for cost in ("25bp", "40bp", "60bp"):
            row = costs.get(cost, {})
            decisive.extend(row.get(name) for name in (
                "cumulative_return_candidate", "cumulative_return_baseline",
                "max_drawdown_candidate", "max_drawdown_baseline",
                "es95_candidate", "es95_baseline",
                "three_limit_down_return_candidate",
                "three_limit_down_return_baseline", "average_exposure_candidate",
                "average_exposure_baseline"))
        if not all(_finite(value) for value in decisive):
            state = "INCONCLUSIVE_HISTORICAL_SCREEN"
        else:
            gates = [
                float(primary["annualized_uplift_pp"]) >=
                float(minimum_annualized_uplift_pp),
                float(primary["calmar_candidate"]) >
                float(primary["calmar_baseline"]),
                float(evidence["bootstrap_20"]["annualized_return"]["lower"]) > 0,
                int(evidence["positive_year_count"]) >= 3,
                float(evidence["top5_positive_profit_share"]) <=
                float(top5_share_max),
                bool(evidence.get("execution_consistency_passed", False)),
            ]
            for cost in ("25bp", "40bp", "60bp"):
                row = costs[cost]
                gates.extend([
                    row["cumulative_return_candidate"] >
                    row["cumulative_return_baseline"],
                    row["max_drawdown_candidate"] >=
                    row["max_drawdown_baseline"],
                    row["es95_candidate"] >= row["es95_baseline"],
                    row["three_limit_down_return_candidate"] >=
                    row["three_limit_down_return_baseline"],
                    abs(row["average_exposure_candidate"]-
                        row["average_exposure_baseline"]) <= 0.01,
                ])
            state = ("PASS_HISTORICAL_SCREEN" if all(gates)
                     else "REJECT_HISTORICAL_SCREEN")
    if state not in FINAL_STATES:
        raise AssertionError("unreachable decision state")
    return ExperimentDecision(
        experiment_id=registration.experiment_id,
        candidate_arm_id=candidate_arm_id,
        baseline_arm_id=registration.baseline_arm_id,
        state=state,
        evidence_sha256=sha256_payload(evidence),
    )


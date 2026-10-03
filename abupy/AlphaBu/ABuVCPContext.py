# -*- encoding: utf-8 -*-
"""Frozen market/industry context overlay and non-mutating shadow decisions."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class VCPContextOverlayConfig:
    overlay_version: str
    base_strategy_version: str
    feature_version: str
    decision_time: str
    execution_time: str
    universe_scope: str
    minimum_market_coverage: float
    normal_breadth_ma120_min: float
    normal_breadth_ma60_min: float
    normal_equal_weight_return_5d_min: float
    normal_new_low_20d_max: float
    retreat_breadth_ma120_max: float
    retreat_breadth_ma60_max: float
    retreat_equal_weight_return_5d_max: float
    retreat_new_low_20d_min: float
    normal_new_risk_multiplier: float
    caution_new_risk_multiplier: float
    retreat_new_risk_multiplier: float
    unknown_new_risk_multiplier: float
    existing_position_policy: str
    industry_rank_field: str
    industry_minimum_coverage: float
    leader_rank_field: str
    leader_minimum_coverage: float
    market_context_overlay: str
    industry_strength_selection: str
    industry_leader_ranking: str

    def __post_init__(self):
        if self.base_strategy_version != "vcp_residual_v2":
            raise ValueError("v1 overlay is frozen to vcp_residual_v2")
        if self.universe_scope != "signal_eligible":
            raise ValueError("context overlay requires signal_eligible breadth")
        for value in (self.minimum_market_coverage,
                      self.industry_minimum_coverage,
                      self.leader_minimum_coverage):
            if not 0 <= value <= 1:
                raise ValueError("coverage thresholds must be in [0, 1]")
        if self.existing_position_policy != "UNCHANGED_EXIT_ENGINE":
            raise ValueError("v1 must preserve the existing exit engine")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


@dataclass(frozen=True)
class MarketContextDecision:
    state: str
    new_risk_multiplier: float
    action: str
    reason_codes: tuple[str, ...]


def load_vcp_context_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(VCPContextOverlayConfig)}
    if set(payload) != expected:
        raise ValueError("VCPContextOverlayConfig fields mismatch")
    return VCPContextOverlayConfig(**payload)


def load_vcp_context_registry(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "registry_version", "status", "registered_at", "base_strategy_version",
        "base_exit_profile", "feature_versions", "historical_diagnostic_period",
        "walk_forward_protocol", "forward_start_date", "forward_definition",
        "minimum_trade_count", "minimum_entry_clusters",
        "minimum_known_market_states", "common_coverage_definition",
        "multiple_testing_family", "multiple_testing_policy",
        "historical_evidence_role", "forward_minimum_months",
        "forward_minimum_entry_clusters", "parameter_search_allowed",
        "hypotheses",
    }
    if set(payload) != required:
        raise ValueError("context experiment registry fields mismatch")
    if payload["status"] != "PREREGISTERED_BEFORE_CONTEXT_RETURN_ANALYSIS":
        raise ValueError("context experiments are not preregistered")
    if payload["parameter_search_allowed"] is not False:
        raise ValueError("parameter search must remain disabled")
    hypotheses = payload["hypotheses"]
    expected_ids = {
        "H1_MARKET_CONTEXT", "H2_INDUSTRY_STRENGTH",
        "H3_WITHIN_INDUSTRY_TREND_LEADER",
    }
    if {item.get("hypothesis_id") for item in hypotheses} != expected_ids:
        raise ValueError("registered hypothesis family mismatch")
    if len(hypotheses) != len(expected_ids):
        raise ValueError("duplicate registered hypothesis")
    payload["registry_sha256"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return payload


def _finite(row, name):
    if row is None or name not in row:
        return None
    try:
        value = float(row[name])
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def evaluate_market_context(row: Mapping | None,
                            config: VCPContextOverlayConfig):
    required = (
        "coverage_ratio", "breadth_above_ma120", "breadth_above_ma60",
        "equal_weight_return_5d", "new_low_20d_ratio",
    )
    values = {name: _finite(row, name) for name in required}
    if (row is None or row.get("universe_scope") != config.universe_scope or
            any(value is None for value in values.values()) or
            values["coverage_ratio"] < config.minimum_market_coverage):
        return MarketContextDecision(
            "UNKNOWN", config.unknown_new_risk_multiplier, "REJECT_NEW_RISK",
            ("MARKET_CONTEXT_UNKNOWN",),
        )
    if (values["breadth_above_ma120"] <= config.retreat_breadth_ma120_max or
            values["breadth_above_ma60"] <= config.retreat_breadth_ma60_max or
            values["equal_weight_return_5d"] <=
            config.retreat_equal_weight_return_5d_max or
            values["new_low_20d_ratio"] >= config.retreat_new_low_20d_min):
        return MarketContextDecision(
            "RETREAT", config.retreat_new_risk_multiplier, "REJECT_NEW_RISK",
            ("MARKET_CONTEXT_RETREAT",),
        )
    if (values["breadth_above_ma120"] >= config.normal_breadth_ma120_min and
            values["breadth_above_ma60"] >= config.normal_breadth_ma60_min and
            values["equal_weight_return_5d"] >=
            config.normal_equal_weight_return_5d_min and
            values["new_low_20d_ratio"] <= config.normal_new_low_20d_max):
        return MarketContextDecision(
            "NORMAL", config.normal_new_risk_multiplier, "ALLOW_NEW_RISK",
            ("MARKET_CONTEXT_NORMAL",),
        )
    return MarketContextDecision(
        "CAUTION", config.caution_new_risk_multiplier, "REDUCE_NEW_RISK",
        ("MARKET_CONTEXT_CAUTION",),
    )


def build_context_shadow(intents: Sequence, market_row: Mapping | None,
                         industry_rows: Mapping[int, Mapping],
                         leader_rows: Mapping[str, Mapping],
                         config: VCPContextOverlayConfig):
    """Return auditable shadow rows without changing input intents or orders."""
    decision = evaluate_market_context(market_row, config)
    rows = []
    for base_rank, intent in enumerate(intents, 1):
        industry_id = int(getattr(intent, "industry_asof", -1))
        industry = industry_rows.get(industry_id)
        leader = leader_rows.get(str(intent.symbol))
        industry_coverage = _finite(industry, "coverage_ratio")
        industry_rank = _finite(industry, config.industry_rank_field)
        leader_coverage = _finite(leader, "coverage_ratio")
        leader_rank = _finite(leader, config.leader_rank_field)
        reasons = list(decision.reason_codes)
        if (industry_rank is None or industry_coverage is None or
                industry_coverage < config.industry_minimum_coverage):
            industry_rank = None
            reasons.append("INDUSTRY_CONTEXT_MISSING")
        if (leader_rank is None or leader_coverage is None or
                leader_coverage < config.leader_minimum_coverage):
            leader_rank = None
            reasons.append("INDUSTRY_LEADER_CONTEXT_MISSING")
        is_sell = getattr(intent, "side", "buy") == "sell"
        rows.append({
            "intent_id": intent.intent_id,
            "signal_asof": int(intent.signal_asof),
            "symbol": str(intent.symbol),
            "side": getattr(intent, "side", "buy"),
            "base_rank": base_rank,
            "base_score": float(getattr(intent, "score", 0.0)),
            "industry_id": industry_id,
            "market_state": decision.state,
            "market_new_risk_multiplier": decision.new_risk_multiplier,
            "market_shadow_action": "ALLOW_EXIT" if is_sell else decision.action,
            "industry_rank_value": industry_rank,
            "leader_rank_value": leader_rank,
            "reason_codes": tuple(dict.fromkeys(reasons)),
        })

    industry_order = sorted(
        range(len(rows)),
        key=lambda pos: (
            rows[pos]["industry_rank_value"] is None,
            -(rows[pos]["industry_rank_value"] or -np.inf),
            -rows[pos]["base_score"], rows[pos]["symbol"],
        ),
    )
    for rank, pos in enumerate(industry_order, 1):
        rows[pos]["industry_shadow_rank"] = rank

    # Preserve the base sequence of industries, replacing only the member
    # order within each industry bucket.
    grouped = {}
    for pos, row in enumerate(rows):
        grouped.setdefault(row["industry_id"], []).append(pos)
    for positions in grouped.values():
        positions.sort(key=lambda pos: (
            rows[pos]["leader_rank_value"] is None,
            -(rows[pos]["leader_rank_value"] or -np.inf),
            rows[pos]["base_rank"], rows[pos]["symbol"],
        ))
    cursors = {industry: 0 for industry in grouped}
    fixed_industry_order = []
    for base in rows:
        industry = base["industry_id"]
        fixed_industry_order.append(grouped[industry][cursors[industry]])
        cursors[industry] += 1
    for rank, pos in enumerate(fixed_industry_order, 1):
        rows[pos]["leader_fixed_industry_shadow_rank"] = rank
    return rows


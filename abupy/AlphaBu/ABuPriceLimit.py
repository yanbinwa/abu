# -*- encoding: utf-8 -*-
"""Versioned A-share daily price-limit rules.

``price_limit_rule`` retains the v1 execution behavior.  New fact generation
must use ``limit_rule_for_context`` so unknown status remains unknown and board
specific risk-warning regimes are evaluated in the correct order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


TICK = Decimal("0.01")


@dataclass(frozen=True)
class PriceLimitRule:
    rule_id: str
    upper_fraction: float | None
    lower_fraction: float | None
    fallback: bool = False
    known: bool = True
    model_version: str = "legacy_v1"
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LimitRuleContext:
    exchange: str
    board: str
    trade_date: int
    security_status: str = "normal"
    listing_stage: str = "seasoned"
    delisting_stage: str = "none"
    special_trading_event: str = "none"
    status_known: bool = True

    def __post_init__(self):
        if self.exchange not in ("sh", "sz"):
            raise ValueError("exchange must be sh or sz")
        if self.board not in ("main", "chinext", "star"):
            raise ValueError("unsupported board")
        if self.security_status not in ("normal", "risk_warning", "unknown"):
            raise ValueError("unsupported security status")
        if self.listing_stage not in (
                "seasoned", "ipo_session_1", "ipo_first_5"):
            raise ValueError("unsupported listing stage")
        if self.delisting_stage not in ("none", "first_day", "subsequent"):
            raise ValueError("unsupported delisting stage")
        if self.special_trading_event not in (
                "none", "relisting_first_day", "other_no_daily_limit"):
            raise ValueError("unsupported special trading event")


PRICE_LIMIT_MODEL_VERSION = "cn_equity_limit_v2_20260706"


def _v2_rule(rule_id, upper, lower, reason_codes=()):
    return PriceLimitRule(
        rule_id, upper, lower, fallback=False, known=True,
        model_version=PRICE_LIMIT_MODEL_VERSION,
        reason_codes=tuple(reason_codes),
    )


def _unknown_rule(reason="UNKNOWN_SECURITY_STATUS"):
    return PriceLimitRule(
        "unknown", None, None, fallback=False, known=False,
        model_version=PRICE_LIMIT_MODEL_VERSION,
        reason_codes=(reason,),
    )


def limit_rule_for_context(context: LimitRuleContext) -> PriceLimitRule:
    """Resolve the PIT rule matrix without inventing missing status facts."""
    if not context.status_known or context.security_status == "unknown":
        return _unknown_rule()

    if context.special_trading_event in (
            "relisting_first_day", "other_no_daily_limit"):
        return _v2_rule(
            "{}_no_limit".format(context.special_trading_event), None, None,
            ("NO_DAILY_LIMIT",),
        )
    if context.delisting_stage == "first_day":
        return _v2_rule(
            "delisting_first_day_no_limit", None, None,
            ("NO_DAILY_LIMIT",),
        )

    date = int(context.trade_date)
    if context.listing_stage in ("ipo_session_1", "ipo_first_5"):
        registration_era = (
            context.board == "star" or
            (context.board == "chinext" and date >= 20200824) or
            (context.board == "main" and date >= 20230410)
        )
        if registration_era:
            return _v2_rule(
                "{}_ipo_first_5_no_limit".format(context.board), None, None,
                ("NO_DAILY_LIMIT",),
            )
        if context.listing_stage == "ipo_session_1":
            return _v2_rule(
                "{}_legacy_ipo_session_1".format(context.board), 0.44, 0.36
            )

    # Delisting stocks use their board's ordinary percentage after day one.
    if context.board == "star":
        return _v2_rule("star_20", 0.20, 0.20)
    if context.board == "chinext":
        if date >= 20200824:
            return _v2_rule("chinext_20", 0.20, 0.20)
        if context.security_status == "risk_warning":
            return _v2_rule("chinext_risk_warning_5", 0.05, 0.05)
        return _v2_rule("chinext_legacy_10", 0.10, 0.10)

    if context.security_status == "risk_warning" and date < 20260706:
        return _v2_rule("main_risk_warning_5", 0.05, 0.05)
    if context.security_status == "risk_warning":
        return _v2_rule("main_risk_warning_10", 0.10, 0.10)
    return _v2_rule("main_10", 0.10, 0.10)


def price_limit_rule(board: str, date: int, is_st: bool,
                     listing_session: int, status_known: bool = True) -> PriceLimitRule:
    """Return the rule known at the open of ``date``.

    ``listing_session`` is one-based. Strategies in this project require long
    histories, but IPO rules are explicit so the execution engine is complete.
    """
    if listing_session <= 0:
        raise ValueError("listing_session must be positive")
    if not status_known:
        return PriceLimitRule("unknown_status_5", 0.05, 0.05, True)
    if is_st:
        return PriceLimitRule("st_5", 0.05, 0.05)
    if board == "star":
        if listing_session <= 5:
            return PriceLimitRule("star_ipo_no_limit", None, None)
        return PriceLimitRule("star_20", 0.20, 0.20)
    if board == "chinext":
        if int(date) >= 20200824:
            if listing_session <= 5:
                return PriceLimitRule("chinext_registration_ipo_no_limit", None, None)
            return PriceLimitRule("chinext_20", 0.20, 0.20)
        if listing_session == 1:
            return PriceLimitRule("chinext_legacy_ipo", 0.44, 0.36)
        return PriceLimitRule("chinext_legacy_10", 0.10, 0.10)
    if board == "main":
        if int(date) >= 20230410 and listing_session <= 5:
            return PriceLimitRule("main_registration_ipo_no_limit", None, None)
        if int(date) < 20230410 and listing_session == 1:
            return PriceLimitRule("main_legacy_ipo", 0.44, 0.36)
        return PriceLimitRule("main_10", 0.10, 0.10)
    return PriceLimitRule("unknown_board_5", 0.05, 0.05, True)


def _money(value) -> float:
    return float(Decimal(str(value)).quantize(TICK, rounding=ROUND_HALF_UP))


def limit_prices(previous_close: float, rule: PriceLimitRule):
    if previous_close <= 0:
        raise ValueError("previous_close must be positive")
    upper = (None if rule.upper_fraction is None else
             _money(Decimal(str(previous_close)) *
                    (Decimal("1") + Decimal(str(rule.upper_fraction)))))
    lower = (None if rule.lower_fraction is None else
             _money(Decimal(str(previous_close)) *
                    (Decimal("1") - Decimal(str(rule.lower_fraction)))))
    return lower, upper


def can_buy_at_open(opening: float, upper_limit: float | None,
                    tick: float = 0.01) -> bool:
    return opening > 0 and (upper_limit is None or opening < upper_limit - tick - 1e-12)


def can_sell_at_open(opening: float, lower_limit: float | None,
                     tick: float = 0.01) -> bool:
    return opening > 0 and (lower_limit is None or opening > lower_limit + tick + 1e-12)

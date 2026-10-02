# -*- encoding: utf-8 -*-
"""Deterministic A-share daily price-limit rules for open-order simulation."""
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

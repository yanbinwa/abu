"""Historical A-share price-limit rule tests."""

import unittest

from abupy.AlphaBu.ABuPriceLimit import (
    LimitRuleContext, can_buy_at_open, can_sell_at_open,
    limit_prices, limit_rule_for_context, price_limit_rule,
)


class PriceLimitTest(unittest.TestCase):

    def test_main_st_star_and_chinext_rules(self):
        self.assertEqual(price_limit_rule("main", 20250102, False, 100).upper_fraction, 0.10)
        self.assertEqual(price_limit_rule("main", 20250102, True, 100).upper_fraction, 0.05)
        self.assertIsNone(price_limit_rule("star", 20250102, False, 5).upper_fraction)
        self.assertEqual(price_limit_rule("star", 20250102, False, 6).upper_fraction, 0.20)
        self.assertEqual(price_limit_rule("chinext", 20200821, False, 100).upper_fraction, 0.10)
        self.assertEqual(price_limit_rule("chinext", 20200824, False, 100).upper_fraction, 0.20)

    def test_unknown_status_falls_back_to_five_percent(self):
        rule = price_limit_rule("main", 20250102, False, 100, status_known=False)
        self.assertTrue(rule.fallback)
        self.assertEqual(rule.upper_fraction, 0.05)

    def test_decimal_rounding_and_directional_open_rules(self):
        lower, upper = limit_prices(10.05, price_limit_rule("main", 20250102, False, 100))
        self.assertEqual(lower, 9.05)
        self.assertEqual(upper, 11.06)
        self.assertFalse(can_buy_at_open(11.05, upper))
        self.assertTrue(can_buy_at_open(11.04, upper))
        self.assertFalse(can_sell_at_open(9.06, lower))
        self.assertTrue(can_sell_at_open(9.07, lower))

    def test_v2_board_specific_risk_warning_rules(self):
        main_before = limit_rule_for_context(LimitRuleContext(
            "sh", "main", 20260703, security_status="risk_warning"))
        main_after = limit_rule_for_context(LimitRuleContext(
            "sh", "main", 20260706, security_status="risk_warning"))
        star = limit_rule_for_context(LimitRuleContext(
            "sh", "star", 20250102, security_status="risk_warning"))
        chinext = limit_rule_for_context(LimitRuleContext(
            "sz", "chinext", 20250102, security_status="risk_warning"))
        self.assertEqual(main_before.upper_fraction, 0.05)
        self.assertEqual(main_after.upper_fraction, 0.10)
        self.assertEqual(star.upper_fraction, 0.20)
        self.assertEqual(chinext.upper_fraction, 0.20)

    def test_v2_registration_ipo_delisting_and_relisting(self):
        for context in (
            LimitRuleContext("sh", "star", 20190722,
                             listing_stage="ipo_first_5"),
            LimitRuleContext("sz", "chinext", 20200824,
                             listing_stage="ipo_first_5"),
            LimitRuleContext("sh", "main", 20230410,
                             listing_stage="ipo_first_5"),
            LimitRuleContext("sz", "main", 20250102,
                             delisting_stage="first_day"),
            LimitRuleContext("sh", "main", 20250102,
                             special_trading_event="relisting_first_day"),
        ):
            rule = limit_rule_for_context(context)
            self.assertTrue(rule.known)
            self.assertIsNone(rule.upper_fraction)
            self.assertIn("NO_DAILY_LIMIT", rule.reason_codes)

        legacy = limit_rule_for_context(LimitRuleContext(
            "sh", "main", 20230101, listing_stage="ipo_session_1"))
        self.assertEqual((legacy.upper_fraction, legacy.lower_fraction),
                         (0.44, 0.36))

    def test_v2_unknown_status_does_not_generate_a_fact(self):
        rule = limit_rule_for_context(LimitRuleContext(
            "sh", "star", 20250102, security_status="unknown",
            status_known=False))
        self.assertFalse(rule.known)
        self.assertIsNone(rule.upper_fraction)
        self.assertEqual(rule.reason_codes, ("UNKNOWN_SECURITY_STATUS",))


if __name__ == "__main__":
    unittest.main()

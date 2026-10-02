"""Historical A-share price-limit rule tests."""

import unittest

from abupy.AlphaBu.ABuPriceLimit import (
    can_buy_at_open, can_sell_at_open, limit_prices, price_limit_rule,
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


if __name__ == "__main__":
    unittest.main()

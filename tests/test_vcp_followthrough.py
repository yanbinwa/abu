"""One-session follow-through confirmation tests."""
import unittest

from abupy.AlphaBu.ABuVCPFollowThrough import confirm_followthrough_intents
from abupy.AlphaBu.ABuVCPStrategy import VCPStrategy
from tests.test_vcp_strategy import make_vcp_panel


class VCPFollowThroughTest(unittest.TestCase):

    def test_confirmation_moves_signal_one_day_and_freezes_adjusted_stop(self):
        panel, day = make_vcp_panel()
        source = VCPStrategy(panel).generate_intents(day, "core")[0]
        column = panel.symbol_index[source.symbol]
        panel.exec_close = panel.exec_close.copy()
        panel.close[day+1, column] = 10.3
        panel.exec_close[day+1, column] = 10.6
        panel.atr21[day+1, column] = .2
        result = confirm_followthrough_intents(panel, [source])
        self.assertEqual(len(result), 1)
        confirmed = result[0]
        self.assertEqual(confirmed.signal_asof, int(panel.dates[day+1]))
        self.assertEqual(confirmed.initial_stop_adjusted,
                         source.initial_stop_adjusted)
        self.assertAlmostEqual(
            confirmed.adjustment_factor_signal, 10.6/10.3)
        self.assertEqual(confirmed.metadata["source_intent_id"], source.intent_id)

    def test_close_back_below_breakout_is_rejected(self):
        panel, day = make_vcp_panel()
        source = VCPStrategy(panel).generate_intents(day, "core")[0]
        column = panel.symbol_index[source.symbol]
        panel.close[day+1, column] = source.metadata["breakout_level"]-.01
        panel.exec_close[day+1, column] = panel.close[day+1, column]
        self.assertEqual(confirm_followthrough_intents(panel, [source]), [])

    def test_t_plus_2_price_is_not_read(self):
        panel, day = make_vcp_panel()
        source = VCPStrategy(panel).generate_intents(day, "core")[0]
        column = panel.symbol_index[source.symbol]
        panel.close[day+1, column] = 10.3
        panel.exec_close[day+1, column] = 10.3
        panel.atr21[day+1, column] = .2
        first = confirm_followthrough_intents(panel, [source])
        panel.open[day+2, column] = 1000
        panel.exec_open[day+2, column] = 1000
        second = confirm_followthrough_intents(panel, [source])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

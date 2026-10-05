import unittest
from types import SimpleNamespace
import numpy as np

from abupy.AlphaBu.ABuAlphaIndustryGate import IndustryExcessEntryGate, IndustryExcessHistory
from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteConfig


class IndustryGateTest(unittest.TestCase):
    def test_gate_preserves_order_and_suppresses_only_rank_exits(self):
        panel = SimpleNamespace(dates=[20240102], symbol_index={'a': 0, 'b': 1, 'c': 2, 'd': 3})
        history = SimpleNamespace(panel=panel, values=lambda day: np.array([-.06, -.05, .02, np.nan]))
        gate = IndustryExcessEntryGate(history)
        exits, entries = gate.filter_review(panel, None, 0, ['held'], ['c', 'a', 'd', 'b'])
        self.assertEqual(exits, [])
        self.assertEqual(entries, ['c', 'b'])
        self.assertEqual([r['allowed'] for r in gate.entry_decisions], [True, False, False, True])
        self.assertEqual(len(gate.decisions), 1)
        # The gate applies even when the base policy proposes no rank exit.
        self.assertEqual(gate.filter_review(panel, None, 0, [], ['a'])[1], [])
        with self.assertRaises(ValueError):
            gate.filter_review(SimpleNamespace(), None, 0, [], ['b'])

    def test_cache_uses_raw_asof_feature_not_percentile_or_future(self):
        source = np.array([[-.02, .2], [-.06, .4]])
        days = []
        def raw(day):
            days.append(day)
            self.assertEqual(day, 10)
            return source
        history = IndustryExcessHistory.__new__(IndustryExcessHistory)
        history.engine = SimpleNamespace(raw_features=raw)
        history.column = 0
        history.cache = {}
        np.testing.assert_array_equal(history.values(10), [-.02, -.06])
        source[:] = 999
        np.testing.assert_array_equal(history.values(10), [-.02, -.06])
        self.assertEqual(days, [10])
        self.assertFalse(history.values(10).flags.writeable)

    def test_actual_feature_is_unchanged_when_future_panel_is_perturbed(self):
        rng = np.random.default_rng(42)
        close = 10*np.exp(np.cumsum(rng.normal(0,.01,(280,3)),axis=0))
        panel = SimpleNamespace(symbols=['a','b','c'], close=close,
            open=close*.999, high=close*1.02, low=close*.98,
            returns=np.vstack([np.zeros(3),close[1:]/close[:-1]-1]),
            amount=np.full((280,3),3e7), ma10=close*.99, ma60=close*.98,
            ma120=close*.97, atr21=close*.02, industry=np.zeros((280,3),int),
            signal_eligible=lambda **kw: np.ones((280,3),bool),
            breadth_denominator=lambda **kw: np.ones((280,3),bool))
        config = Alpha158LiteConfig()
        before = IndustryExcessHistory(panel,config).values(260)
        returns = close[260]/close[240]-1
        np.testing.assert_allclose(before,returns-returns.mean(),atol=1e-8)
        for name in ('close','open','high','low','returns','amount','ma10','ma60','ma120','atr21','industry'):
            getattr(panel,name)[261:] = 9999
        after = IndustryExcessHistory(panel,config).values(260)
        np.testing.assert_array_equal(before,after)


if __name__ == '__main__':
    unittest.main()

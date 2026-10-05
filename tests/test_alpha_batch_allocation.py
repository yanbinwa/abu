import unittest
from dataclasses import replace
from types import SimpleNamespace
import numpy as np

from abupy.AlphaBu.ABuAlphaBatchAllocation import BatchRiskAllocator
from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine, RiskConfig
from abupy.AlphaBu.ABuPortfolioExecutor import PortfolioExecutor, ExecutionConfig
from tests.test_portfolio_risk import make_panel, make_intent, liberal


class BatchAllocationTest(unittest.TestCase):
    def setup_case(self, config=None):
        panel=make_panel()
        executor=PortfolioExecutor(panel,ExecutionConfig())
        executor.last_close=panel.exec_close[125].copy()
        risk=PortfolioRiskEngine(panel,config or replace(RiskConfig(),same_day_new_risk_fraction=.003))
        intents=[make_intent(panel,symbol=s,suffix=str(i)) for i,s in enumerate(panel.symbols)]
        features=SimpleNamespace(make_intent=lambda day,col,score:intents[col])
        rows=[dict(symbol=s,column=i,daily_rank=i+1,alpha_score=3-i) for i,s in enumerate(panel.symbols)]
        return panel,executor,risk,features,rows

    def test_equal_plan_is_order_independent_and_does_not_mutate_account(self):
        panel,executor,risk,features,rows=self.setup_case()
        allocator=BatchRiskAllocator(diagnose=True)
        result=allocator.prepare(rows,3,features,risk,executor,125,126,set(),'alpha_score')
        self.assertEqual(executor.orders,[])
        self.assertEqual(executor.reservations,{})
        self.assertEqual(executor.position_ledger.logical_trades,{})
        self.assertEqual(executor.reserved_cash,0)
        self.assertEqual(len(risk.decisions),0)
        for item in allocator.order_diagnostics:
            self.assertEqual(item['equal_risk_rank'],item['equal_risk_reverse'])
            self.assertEqual(item['equal_risk_rank'],item['equal_risk_fixed_hash'])
        self.assertTrue(any(x['sequential_rank']!=x['sequential_reverse'] for x in allocator.order_diagnostics))
        self.assertEqual(len({quantity for _,_,quantity in result}),1)
        self.assertLessEqual(sum(x['planned_risk_cash'] for x in allocator.plans),3000)

    def test_pool_selection_is_rank_based_then_fixed_before_submission(self):
        _,executor,risk,features,rows=self.setup_case()
        allocator=BatchRiskAllocator(submission_order='reverse')
        result=allocator.prepare(rows[::-1],2,features,risk,executor,125,126,set(),'alpha_score')
        self.assertEqual([row['daily_rank'] for row,_,_ in result],[2,1])
        empty=allocator.prepare(rows,0,features,risk,executor,125,126,set(),'alpha_score')
        self.assertEqual(empty,[])

    def test_joint_stress_shrink_preserves_all_existing_limits(self):
        cfg=liberal(single_trade_risk_fraction=.04,portfolio_open_risk_fraction=.1,
                    same_day_new_risk_fraction=.1,max_stress_loss_fraction=.025)
        _,executor,risk,features,rows=self.setup_case(cfg)
        allocator=BatchRiskAllocator(diagnose=True)
        result=allocator.prepare(rows,3,features,risk,executor,125,126,set(),'alpha_score')
        self.assertTrue(all(x['planned_quantity']<x['individual_cap_quantity'] for x in allocator.plans))
        for _,intent,quantity in result[::-1]:
            order,_,decision=risk.approve(executor,intent,125,126,requested_quantity=quantity)
            self.assertEqual(order.quantity,quantity)
            self.assertLessEqual(decision.max_stress_loss_cash,25000+1e-9)

    def test_board_lot_rounding_never_redistributes_remainder_to_first_symbol(self):
        cfg=replace(RiskConfig(),same_day_new_risk_fraction=.0001)
        _,executor,risk,features,rows=self.setup_case(cfg)
        allocator=BatchRiskAllocator()
        self.assertEqual(allocator.prepare(rows,3,features,risk,executor,125,126,set(),'alpha_score'),[])


if __name__=='__main__':
    unittest.main()

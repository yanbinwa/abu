"""Prospective chronology, paired isolation, checkpoint and replay invariants."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from datetime import date
from unittest.mock import patch
from dataclasses import replace
import numpy as np
import pandas as pd

from abupy.AlphaBu import ABuAlphaForwardShadow as shadow
from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteConfig, Alpha158LiteLowTurnoverConfig
from abupy.AlphaBu.ABuPortfolioRisk import RiskConfig
from tests.test_vcp_strategy import make_vcp_panel
from scripts.run_alpha158_forward_shadow_v1 import validate_capture, evaluate, run_session


class ConstantModel:
    def predict(self,frame):
        return np.arange(len(frame),dtype=float)


def truncate(panel,length):
    result=copy.deepcopy(panel)
    total=len(panel.dates)
    for item in (result.base,result):
        for name,value in tuple(vars(item).items()):
            if isinstance(value,np.ndarray) and value.ndim and value.shape[0]==total:
                setattr(item,name,value[:length].copy())
    return result


def fixture():
    panel,_=make_vcp_panel()
    panel.amount[:]=1e9
    panel.base.corporate_actions={}
    source=Alpha158LiteConfig(minimum_history_sessions=120,minimum_median_amount_20d=1,
                             stagnation_sessions=200)
    policy=Alpha158LiteLowTurnoverConfig(source_config_sha256=source.sha256,
        review_interval_sessions=1,entry_persistence_reviews=1,exit_persistence_reviews=1,
        minimum_rank_exit_holding_sessions=1,entry_rank_limit=1,retention_rank_limit=1)
    protocol=json.loads((Path(__file__).resolve().parents[1]/'configs/selection/alpha158_forward_shadow_v1.json').read_text())
    protocol['minimum_candidate_rows']=1
    state=shadow.new_state(truncate(panel,240),source,policy,RiskConfig(),protocol,'2023-12-01T16:00:00+08:00')
    return panel,state


def scores(panel,state):
    return shadow.feature_scores(panel,state['source'],ConstantModel(),1)


class ForwardShadowTest(unittest.TestCase):
    def test_training_uses_strictly_mature_labels_without_parameter_search(self):
        panel,state=fixture()
        source=replace(state['source'],minimum_train_dates=2)
        protocol=dict(state['protocol'],initial_training_start=20230101)
        snapshots=[]
        class Engine:
            def __init__(self,*args):
                pass
            def snapshot(self,day,include_labels):
                snapshots.append(day)
                return pd.DataFrame({'target_rank':[1.]})
        class Model:
            def __init__(self,*args):
                self.manifest={}
            def fit(self,*args):
                return self
        with patch.object(shadow,'Alpha158LiteFeatureEngine',Engine),patch.object(shadow,'Alpha158LiteModel',Model):
            model=shadow.train_once(panel,source,protocol)
        self.assertTrue(all(day+20<len(panel.dates)-1 for day in snapshots))
        self.assertTrue(all(b-a==source.training_date_stride for a,b in zip(snapshots,snapshots[1:])))
        self.assertLess(model.manifest['maximum_label_end'],int(panel.dates[-1]))

    def test_initialization_has_no_backfilled_orders(self):
        panel,state=fixture()
        self.assertEqual(state['sessions'],0)
        for account in state['accounts'].values():
            self.assertEqual(account['executor'].orders,[])
            self.assertEqual(account['executor'].fills,[])
            self.assertEqual(account['executor'].cash,1e6)

    def test_close_order_waits_for_next_open_and_checkpoint_preserves_reservation(self):
        panel,state=fixture(); first=truncate(panel,241)
        shadow.step_accounts(state,first,scores(first,state))
        for account in state['accounts'].values():
            self.assertEqual(len(account['executor'].fills),0)
            self.assertTrue(account['executor'].orders)
            self.assertTrue(all(o.valid_session==0 for o in account['executor'].orders))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.pkl.gz'
            shadow.dump_pickle(path,shadow.checkpoint_state(state))
            saved=shadow.load_pickle(path)
        second=truncate(panel,242)
        shadow.step_accounts(saved,second,scores(second,saved))
        for account in saved['accounts'].values():
            fills=[f for f in account['executor'].fills if f.status=='filled']
            self.assertTrue(fills)
            self.assertTrue(all(f.date==int(second.dates[-1]) for f in fills))
        with self.assertRaisesRegex(ValueError,'already processed'):
            shadow.step_accounts(saved,second,scores(second,saved))

    def test_only_rank_exit_differs_and_protective_exit_still_applies(self):
        panel,state=fixture()
        for length in (241,242):
            current=truncate(panel,length)
            shadow.step_accounts(state,current,scores(current,state))
        current=truncate(panel,243)
        daily=scores(current,state)
        daily['daily_rank']=[2,1]
        shadow.step_accounts(state,current,daily)
        self.assertTrue(any(o.side=='sell' for o in state['accounts']['baseline']['executor'].orders))
        self.assertFalse(any(o.side=='sell' for o in state['accounts']['event_exit_only']['executor'].orders))
        next_panel=truncate(panel,244)
        next_panel.base.close[-1,:]=1
        shadow.step_accounts(state,next_panel,scores(next_panel,state))
        self.assertTrue(any(o.side=='sell' for o in state['accounts']['event_exit_only']['executor'].orders))

    def test_gaps_and_revisions_rejected(self):
        panel,_=fixture(); old=truncate(panel,240); new=truncate(panel,241)
        shadow.assert_append_only(old,new)
        with self.assertRaisesRegex(ValueError,'one new session'):
            shadow.assert_append_only(old,truncate(panel,242))
        new.base.close[0,0]+=1
        with self.assertRaisesRegex(ValueError,'historical panel revision'):
            shadow.assert_append_only(old,new)

    def test_checkpoint_uses_fixed_dates_and_clusters_in_both_arms(self):
        _,state=fixture(); state['first_forward_date']=20230101
        state['last_processed_date']=20230630
        for account in state['accounts'].values():
            account['executor'].fills=[SimpleNamespace(date=i,side='buy',status='filled') for i in range(30)]
        shadow.update_checkpoint(state)
        self.assertEqual(state['evaluation_status'],'COLLECTING_NO_EFFICACY_DECISION')
        state['last_processed_date']=20230701
        state['accounts']['event_exit_only']['executor'].fills=[]
        shadow.update_checkpoint(state)
        self.assertEqual(state['evaluation_checkpoint'],1)
        state['last_processed_date']=20230901
        state['accounts']['event_exit_only']['executor'].fills=state['accounts']['baseline']['executor'].fills.copy()
        shadow.update_checkpoint(state)
        self.assertEqual(state['evaluation_status'],'COLLECTING_NO_EFFICACY_DECISION')
        state['last_processed_date']=20231001
        shadow.update_checkpoint(state)
        self.assertEqual(state['evaluation_status'],'REVIEW_REQUIRED')

    def test_future_cash_payment_preserved_and_credited_once(self):
        panel,state=fixture()
        for length in (241,242):
            current=truncate(panel,length)
            shadow.step_accounts(state,current,scores(current,state))
        current=truncate(panel,243)
        held=next(iter(state['accounts']['baseline']['executor'].positions))
        record=int(current.dates[-1]); ex=int(panel.dates[243]); payout=int(panel.dates[245])
        raw=pd.DataFrame([dict(symbol=held,股权登记日=str(record),除权日=str(ex),派息日=str(payout),派息比例=1)])
        shadow.forward_actions(current,raw)
        self.assertEqual(current.corporate_actions[242][0]['cash_day'],-payout)
        shadow.step_accounts(state,current,scores(current,state))
        executor=state['accounts']['baseline']['executor']
        quantity=executor.positions[held].quantity
        self.assertEqual(executor.curve[-1]['dividend_receivable'],0)
        before=executor.cash
        for length in range(244,248):
            current=truncate(panel,length)
            shadow.forward_actions(current,raw)
            shadow.step_accounts(state,current,scores(current,state))
            self.assertAlmostEqual(executor.curve[-1]['dividend_receivable'],quantity*.08 if length<246 else 0)
        self.assertAlmostEqual(executor.cash-before,quantity*.08)
        self.assertEqual(sum(e.event_type=='CASH_DIVIDEND' for e in executor.position_events),1)

    def test_conflicting_corporate_actions_rejected(self):
        panel,_=fixture(); day=str(panel.dates[240])
        rows=[dict(symbol=panel.symbols[0],股权登记日=day,除权日=str(panel.dates[241]),派息比例=x) for x in (1,2)]
        with self.assertRaisesRegex(ValueError,'conflicting'):
            shadow.forward_actions(panel,pd.DataFrame(rows))

    def test_unhandled_stock_distribution_stops_before_commit(self):
        panel,state=fixture()
        for length in (241,242):
            current=truncate(panel,length)
            shadow.step_accounts(state,current,scores(current,state))
        current=truncate(panel,243)
        held=next(iter(state['accounts']['baseline']['executor'].positions))
        shadow.forward_actions(current,pd.DataFrame([dict(symbol=held,股权登记日=str(current.dates[-1]),
            除权日=str(panel.dates[243]),股份到账日=str(panel.dates[244]),送股比例=1)]))
        with self.assertRaisesRegex(ValueError,'entitlement ledger'):
            shadow.step_accounts(state,current,scores(current,state))

    def test_stale_index_cannot_label_current_spot_as_yesterdays_close(self):
        from scripts import update_paper_market_data as updater
        with tempfile.TemporaryDirectory() as tmp,patch.object(updater,'_index_rows',return_value={
                'sh000300':pd.DataFrame({'date_number':[20240102]})}),patch.object(updater,'_signal_path',return_value=Path(tmp)/'benchmark'),patch.object(updater,'_csv_header_and_last',return_value=([],{'date':'20231229'})):
            with self.assertRaisesRegex(RuntimeError,'same-day index'):
                updater.update_market_data(tmp,tmp,tmp,today=date(2024,1,3),required_trade_date=20240103)

    def test_archive_chain_replay_empty_then_filled_and_tamper_detection(self):
        panel,state=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); stage=root/'stage'; stage.mkdir()
            shadow.dump_pickle(stage/'state.pkl.gz',shadow.checkpoint_state(state))
            shadow.dump_pickle(stage/'model.pkl.gz',ConstantModel())
            shadow.dump_pickle(stage/'panel.pkl.gz',truncate(panel,240))
            shadow.export_accounts(stage,state)
            shadow.commit_directory(stage,root/'genesis','GENESIS')
            previous=shadow.verify_chain(root)[1]
            previous_panel=truncate(panel,240)
            for length in (241,242,243):
                current=truncate(panel,length); daily=scores(current,state)
                shadow.step_accounts(state,current,daily)
                stage=root/'stage'; stage.mkdir()
                shadow.dump_pickle(stage/'panel_delta.pkl.gz',shadow.panel_delta(previous_panel,current))
                previous_panel=current
                shadow.dump_pickle(stage/'state.pkl.gz',shadow.checkpoint_state(state))
                daily.to_csv(stage/'features_scores.csv.gz',index=False,float_format='%.17g')
                shadow.export_accounts(stage,state)
                target=root/'sessions'/str(current.dates[-1])
                shadow.commit_directory(stage,target,previous)
                previous=shadow.verify_chain(root)[1]
            self.assertEqual(shadow.audit_replay(root)['sessions'],3)
            restored=shadow.last_panel(shadow.verify_chain(root)[0])
            np.testing.assert_equal(restored.close,truncate(panel,243).close)
            with patch('scripts.run_alpha158_forward_shadow_v1.shadow.now_shanghai',return_value=pd.Timestamp(str(panel.dates[242]),tz='Asia/Shanghai')):
                self.assertEqual(run_session(root)['status'],'ALREADY_COMMITTED')
            self.assertEqual(evaluate(root)['status'],'EVALUATION_NOT_DUE')
            (target/'summary.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'integrity failure'):
                shadow.verify_chain(root)

    def test_capture_rejects_late_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'raw_batches/one').mkdir(parents=True)
            pd.DataFrame([dict(date=20240102,symbol='sz000001')]).to_csv(root/'stock_spot.csv',index=False)
            path=root/'raw_batches/one/metadata.json'
            shadow.json_write(path,dict(trade_date=20240102,collected_at='2024-01-03T16:00:00+08:00'))
            with self.assertRaisesRegex(ValueError,'same-day close'):
                validate_capture(root,20240102,'2024-01-01T00:00:00+08:00',1)


if __name__=='__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_alpha158_daily_service as service


def payload():
    return dict(summary=dict(sessions=1,accounts={service.ARM:dict(capital=1000000.,return_pct=0.,max_drawdown_pct=0.)}),
        market_date=20261009,cash=1000000.,exposure_pct=0.,names={'sz000001':'测试股票'},
        review_day=True,positions=[],decisions=[],fills=[],
        pending=[dict(symbol='sz000001',side='buy',quantity=100,max_buy_price_raw=10.,initial_stop_raw=9.)],
        picks=[dict(symbol='sz000001',name='测试股票',rank=1,score=.123)])


class AlphaDailyServiceTest(unittest.TestCase):
    def test_planned_trade_is_not_reported_as_filled(self):
        text=service.report(payload())
        self.assertIn('无成交或拒单记录',text)
        self.assertIn('尚未成交',text)
        self.assertIn('不是买入委托',text)
        self.assertIn('最高买价 ¥10.000',text)
        self.assertNotIn('成交价 ¥10.000',text)

    def test_actual_exit_and_rejected_entry_are_distinguished(self):
        data=payload();data['pending']=[]
        data['fills']=[dict(symbol='sz000001',side='sell',quantity=100,status='filled',
                           fill_price_raw=9.5,exit_reason='TRAILING_STOP'),
                       dict(symbol='sz000001',side='buy',quantity=0,status='rejected',reason_code='LIMIT_UP')]
        text=service.report(data)
        self.assertIn('移动止损',text)
        self.assertIn('未成交：rejected / LIMIT_UP',text)
        self.assertIn('无买卖计划',text)

    def test_queue_retries_recognize_pending_and_sent_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'outbox'
            self.assertEqual(service.queue_once('消息','stable',out,'owner')['status'],'queued')
            self.assertEqual(service.queue_once('消息','stable',out,'owner')['status'],'queued')
            self.assertEqual(len(list(out.glob('*.json'))),1)
            (out/'stable.json').rename(Path(tmp)/'sent-stable.json')
            self.assertEqual(service.queue_once('消息','stable',out,'owner')['status'],'sent')
            self.assertEqual(len(list(out.glob('*.json'))),0)
            with self.assertRaises(ValueError):service.queue_once('不同消息','stable',out,'owner')
            with self.assertRaises(ValueError):service.queue_once('消息','stable',out,'different-owner')

    def test_long_chinese_report_is_utf8_bounded_without_data_loss(self):
        text='每日模拟盘\n'*500
        chunks=service.split_message(text)
        self.assertTrue(all(len(x.encode('utf-8'))<1800 for x in chunks))
        self.assertEqual(''.join(x.split('\n',1)[1] for x in chunks),text.strip())

    def test_no_current_session_cannot_publish_old_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(service,'verify_service',return_value=({'forward_root':'/unused'},'owner')), \
                 patch.object(service,'frozen_command',return_value=dict(status='NO_SAME_DAY_SNAPSHOT',
                     summary=dict(sessions=0,last_processed_date=20260930))), \
                 patch.object(service,'is_trading_session',return_value=False), \
                 patch.object(service,'load_payload',side_effect=AssertionError('must not read old report')):
                result=service.operate(root,'run')
            self.assertEqual(result['queued'],0)
            self.assertEqual(result['status'],'NON_TRADING_DAY')

    def test_stale_market_data_on_a_trading_day_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(service,'verify_service',return_value=({'forward_root':'/unused'},'owner')), \
                 patch.object(service,'frozen_command',return_value=dict(status='NO_SAME_DAY_SNAPSHOT',
                     summary=dict(sessions=0,last_processed_date=20260930))), \
                 patch.object(service,'is_trading_session',return_value=True):
                with self.assertRaisesRegex(RuntimeError,'no current close snapshot'):
                    service.operate(Path(tmp),'run')


if __name__=='__main__':unittest.main()

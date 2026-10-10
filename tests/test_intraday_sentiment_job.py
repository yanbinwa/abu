import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from abupy.ServiceBu import (
    IntradaySentimentSnapshotJob, OperationalStore, WholeMarketQuoteClient,
)


class _Response(object):
    status_code = 200

    def __init__(self, text):
        self.content = text.encode("gb18030")

    def raise_for_status(self):
        return None


class _Session(object):

    def __init__(self, text):
        self.text = text
        self.urls = []

    def get(self, url, **unused_kwargs):
        self.urls.append(url)
        return _Response(self.text)


def quote_frame(source):
    return pd.DataFrame({
        "symbol": ["sh600000", "sz000001"],
        "name": ["浦发银行", "平安银行"],
        "last": [10.1, 9.9], "pre_close": [10.0, 10.0],
        "open": [10.0, 10.0], "high": [10.2, 10.1],
        "low": [9.9, 9.8], "volume": [10000.0, 20000.0],
        "amount": [101000.0, 198000.0],
        "provider_time": pd.to_datetime([
            "2026-10-09 10:00:00", "2026-10-09 10:00:00"]),
        "change_pct": [.01, -.01], "turnover_rate": [.001, .002],
        "pe": [10.0, 11.0], "pb": [1.0, 1.1],
        "limit_up": [11.0, 11.0], "limit_down": [9.0, 9.0],
        "volume_ratio": [1.2, .8], "source": [source, source],
    })


class _Client(object):

    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def snapshot(self, symbols):
        self.calls.append(tuple(symbols))
        return self.frame.copy()


class IntradaySentimentJobTest(unittest.TestCase):

    def test_tencent_and_sina_parsers_use_provider_units(self):
        fields = [""] * 54
        fields[1] = "浦发银行"; fields[2] = "600000"
        fields[3] = "10.10"; fields[4] = "10.00"; fields[5] = "9.99"
        fields[6] = "123"; fields[30] = "20261009100000"
        fields[32] = "1.00"; fields[33] = "10.20"; fields[34] = "9.90"
        fields[37] = "12.3"; fields[38] = "0.50"; fields[39] = "10"
        fields[46] = "1.2"; fields[47] = "11"; fields[48] = "9"
        fields[49] = "1.1"
        tencent = WholeMarketQuoteClient(
            "tencent", session=_Session('v_sh600000="{}";'.format(
                "~".join(fields))))
        tx = tencent.snapshot(["sh600000"])
        self.assertEqual(12_300.0, tx.iloc[0].volume)
        self.assertEqual(123_000.0, tx.iloc[0].amount)

        sina_fields = [""] * 32
        sina_fields[0] = "浦发银行"; sina_fields[1] = "9.99"
        sina_fields[2] = "10.00"; sina_fields[3] = "10.10"
        sina_fields[4] = "10.20"; sina_fields[5] = "9.90"
        sina_fields[8] = "12300"; sina_fields[9] = "123000"
        sina_fields[30] = "2026-10-09"; sina_fields[31] = "10:00:00"
        sina = WholeMarketQuoteClient(
            "sina", session=_Session('var hq_str_sh600000="{}";'.format(
                ",".join(sina_fields))))
        sn = sina.snapshot(["sh600000"])
        self.assertEqual(12_300.0, sn.iloc[0].volume)
        self.assertAlmostEqual(.01, sn.iloc[0].change_pct)

    def test_job_archives_append_only_sentiment_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.csv"
            pd.DataFrame({
                "symbol": ["sh600000", "sz000001"],
                "status": ["listed", "listed"],
            }).to_csv(master, index=False)
            calendar = root / "calendar.json"
            calendar.write_text(json.dumps({"dates": [20261009]}))
            industry = root / "industry.csv"
            pd.DataFrame({
                "symbol": ["sh600000", "sz000001"],
                "分类标准": ["申银万国行业分类标准"] * 2,
                "行业大类": ["银行", "银行"],
                "变更日期": ["2020-01-01", "2020-01-01"],
            }).to_csv(industry, index=False)
            config = root / "config.json"
            config.write_text(json.dumps({
                "execution_mode": "DATA_ONLY", "first_eligible_session": 20261009,
                "security_master_path": str(master),
                "industry_history_path": str(industry),
                "industry_classification_standard": "申银万国行业分类标准",
                "trading_calendar_path": str(calendar),
                "collection_windows": [{"start": "09:31:00", "end": "15:01:30"}],
                "request_timeout_seconds": 1, "tencent_requests_per_second": 2,
                "sina_requests_per_second": .5, "minimum_coverage_ratio": .9,
                "minimum_fresh_ratio": .9, "stale_after_seconds": 180,
                "minimum_industry_coverage_ratio": .9,
            }))
            store = OperationalStore(root / "state.sqlite3")
            clock = lambda: datetime(
                2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            job = IntradaySentimentSnapshotJob(
                store, root / "content", config, clock=clock,
                primary_client=_Client(quote_frame("tencent_market_snapshot")),
                reference_client=_Client(quote_frame("sina_market_snapshot")))
            try:
                first = job()
                second = job()
            finally:
                store.close()
            self.assertEqual("COMMITTED", first["status"])
            self.assertNotEqual(first["manifest_sha256"], second["manifest_sha256"])
            second_manifest = json.loads(Path(
                second["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(2, second_manifest["sequence_no"])
            self.assertEqual(
                first["manifest_sha256"],
                second_manifest["previous_manifest_sha256"])
            self.assertEqual(1, second_manifest["market_metrics"]["advancers"])
            self.assertEqual(1, second_manifest["market_metrics"]["decliners"])
            self.assertEqual("银行", second_manifest["industry_metrics"][0]["industry"])
            self.assertEqual(2, second_manifest["industry_metrics"][0]["quote_count"])
            self.assertEqual([], second["output_snapshot_ids"])


if __name__ == "__main__":
    unittest.main()

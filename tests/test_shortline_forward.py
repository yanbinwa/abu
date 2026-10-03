"""M6 immutable forward capture and bitemporal taxonomy tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pandas as pd

from abupy.AlphaBu.ABuShortLineEvents import (
    ImmutableShortLineSnapshotStore, ThemeTaxonomyMapping, ThemeTaxonomyStore,
)
from scripts.collect_shortline_events import DATASETS, collect, normalize_event_pool
from scripts.audit_shortline_forward import audit


SHANGHAI = ZoneInfo("Asia/Shanghai")


def provider_frame():
    return pd.DataFrame([{
        "代码": "000001", "名称": "测试证券", "最新价": 11.0,
        "成交额": 1000000, "流通市值": 100000000, "换手率": 1.0,
        "封板资金": 10000, "封单资金": 9000, "首次封板时间": "100000",
        "最后封板时间": "140000", "昨日封板时间": "143000",
        "炸板次数": 1, "开板次数": 1, "连板数": 2, "连续跌停": 1,
        "昨日连板数": 1, "涨停价": 11.0, "涨停统计": "2/2",
        "所属行业": "银行", "入选理由": "测试原因",
    }])


class FakeAkShare:
    def __getattr__(self, name):
        if name in DATASETS:
            return lambda date: provider_frame()
        raise AttributeError(name)


class ShortLineForwardTest(unittest.TestCase):

    def test_same_payload_keeps_both_raw_batches_and_reuses_normalization(self):
        with TemporaryDirectory() as directory:
            store = ImmutableShortLineSnapshotStore(directory)
            frame = provider_frame()
            normalized = normalize_event_pool(
                frame, "stock_zt_pool_em", 20261009,
                "2026-10-09T15:21:00+08:00")
            first = store.write(
                frame, source="test", dataset="stock_zt_pool_em",
                trade_date=20261009, ingested_at="2026-10-09T15:21:00+08:00",
                nonce="one", required_columns=DATASETS["stock_zt_pool_em"]["required"],
                normalized=normalized)
            second = store.write(
                frame, source="test", dataset="stock_zt_pool_em",
                trade_date=20261009, ingested_at="2026-10-09T15:22:00+08:00",
                nonce="two", required_columns=DATASETS["stock_zt_pool_em"]["required"],
                normalized=normalized)
            self.assertEqual(first["normalization_status"], "written")
            self.assertEqual(second["normalization_status"], "reused")
            self.assertEqual(second["normalized_path"], first["normalized_path"])
            self.assertEqual(len(list(Path(directory).glob(
                "20261009/stock_zt_pool_em/*/provider_frame.json"))), 2)

    def test_schema_drift_is_not_asof_eligible(self):
        with TemporaryDirectory() as directory:
            meta = ImmutableShortLineSnapshotStore(directory).write(
                pd.DataFrame([{"代码": "000001"}]), source="test",
                dataset="stock_zt_pool_em", trade_date=20261009,
                ingested_at="2026-10-09T15:21:00+08:00", nonce="schema",
                required_columns=("代码", "名称"))
            self.assertEqual(meta["status"], "schema_error")
            self.assertFalse(meta["asof_feature_allowed"])
            self.assertFalse(meta["normalized_path"])

    def test_semantic_proxy_can_be_asof_visible_but_strategy_blocked(self):
        with TemporaryDirectory() as directory:
            meta = ImmutableShortLineSnapshotStore(directory).write(
                pd.DataFrame([{"代码": "000001", "名称": "测试"}]),
                source="test", dataset="auction_quote_snapshot",
                trade_date=20261009,
                ingested_at="2026-10-09T09:26:30+08:00", nonce="proxy",
                required_columns=("代码", "名称"),
                source_semantics="09:26_full_market_quote_proxy",
                quality_codes=("PROXY_NOT_EXACT_AUCTION_FEED",),
                strategy_feature_allowed=False)
            self.assertTrue(meta["asof_feature_allowed"])
            self.assertFalse(meta["strategy_feature_allowed"])

    def test_capture_rejects_naive_clock(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "timezone"):
                collect(
                    trade_date=20261009, phase="close", output_dir=directory,
                    paper_dir=directory, now=datetime(2026, 10, 9, 15, 30),
                    ak_module=FakeAkShare(), calendar_dates={20261009})

    def test_non_trading_day_writes_run_evidence_but_no_event_fact(self):
        with TemporaryDirectory() as directory:
            result = collect(
                trade_date=20261010, phase="close", output_dir=directory,
                paper_dir=directory,
                now=datetime(2026, 10, 10, 15, 30, tzinfo=SHANGHAI),
                ak_module=FakeAkShare(), calendar_dates={20261009})
            self.assertEqual(result["status"], "skipped_non_trading_day")
            self.assertFalse(list(Path(directory).glob(
                "20261010/*/*/metadata.json")))
            run_files = list(Path(directory).glob("_runs/20261010/*.json"))
            self.assertEqual(len(run_files), 1)

    def test_close_capture_is_shadow_only_and_all_datasets_are_traceable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            market = root / "paper/market_snapshots/20261009"
            market.mkdir(parents=True)
            (market / "stock_spot.csv").write_text("x\n1\n")
            (market / "limit_reference.csv").write_text("x\n1\n")
            result = collect(
                trade_date=20261009, phase="close",
                output_dir=root / "forward", paper_dir=root / "paper",
                now=datetime(2026, 10, 9, 15, 30, tzinfo=SHANGHAI),
                ak_module=FakeAkShare(), calendar_dates={20261009})
            self.assertEqual(result["status"], "captured")
            self.assertEqual(result["asof_eligible_count"], len(DATASETS))
            self.assertEqual(result["feature_mode"], "shadow_only")
            self.assertEqual(len(result["market_snapshot_evidence"]), 2)
            self.assertEqual(len(list((root / "forward").glob(
                "20261009/*/*/metadata.json"))), len(DATASETS))
            audit_rows, audit_report = audit(root / "forward")
            self.assertEqual(audit_report["status"], "passed")
            self.assertEqual(len(audit_rows), len(DATASETS))

    def test_taxonomy_mapping_is_invisible_before_mapping_available_at(self):
        mapping = ThemeTaxonomyMapping(
            source="provider", source_theme_id="ai", source_theme_name="AI",
            canonical_theme_id="theme_ai", canonical_theme_name="人工智能",
            taxonomy_version="taxonomy_v1", valid_from=20261001,
            valid_to=99991231,
            mapping_available_at="2026-10-09T18:00:00+08:00")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "taxonomy.csv"
            ThemeTaxonomyStore.write_snapshot(path, [mapping])
            store = ThemeTaxonomyStore([path])
            before = store.resolve(
                "provider", "ai", 20261009, "2026-10-09T17:59:59+08:00")
            after = store.resolve(
                "provider", "ai", 20261009, "2026-10-09T18:00:00+08:00")
            retrospective = store.resolve(
                "provider", "ai", 20261009, "2026-10-09T17:59:59+08:00",
                retrospective=True)
            self.assertTrue(before.empty)
            self.assertEqual(len(after), 1)
            self.assertEqual(len(retrospective), 1)
            with self.assertRaises(FileExistsError):
                ThemeTaxonomyStore.write_snapshot(path, [mapping])


if __name__ == "__main__":
    unittest.main()

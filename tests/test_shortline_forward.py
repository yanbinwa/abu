"""M6 immutable forward capture and bitemporal taxonomy tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

from abupy.AlphaBu.ABuShortLineEvents import (
    ImmutableShortLineSnapshotStore, ThemeTaxonomyMapping, ThemeTaxonomyStore,
    load_shortline_forward_policy, read_forward_anchor,
)
from scripts.collect_shortline_events import (
    DATASETS, ELTDX_DATASET, collect, normalize_eltdx_limit_events,
    normalize_event_pool,
)
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


class FakeAuctionAkShare(FakeAkShare):
    def stock_zh_a_spot_em(self):
        row = provider_frame().iloc[0].to_dict()
        row.update({"今开": 10.5, "昨收": 10.0, "成交量": 100000})
        return pd.DataFrame([row] * 3000)


class PartialAkShare(FakeAkShare):
    def __getattr__(self, name):
        if name == "stock_zt_pool_strong_em":
            return lambda date: pd.DataFrame()
        return super().__getattr__(name)


class FakeEltdxRow:
    def __init__(self, code="000001", status="limit_up"):
        self.code = code
        self.full_code = "sz" + code
        self.name = "测试证券"
        self.status = status
        self.board_level = 2
        self.highest_board_level = 3
        self.industry = "银行"
        self.limit_reason = "并购重组"
        self.limit_reason_extra = "供应商补充原因"
        self.seal_amount = 123456
        self.limit_time = "10:00:00"
        self.broken_count = 1
        self.raw = {
            "rqex": "20260930", "ZQDM": code, "SC": "0",
            "ZQJC": self.name, "ztlb": status, "lbts": 2, "zglb": 3,
            "ztyy": self.limit_reason, "fde": self.seal_amount,
            "ztsj": self.limit_time, "kbcs": self.broken_count,
            "sshy": self.industry,
        }


class FakeEltdxResult:
    def __init__(self):
        self.rows = [
            FakeEltdxRow("000001", "limit_up"),
            FakeEltdxRow("600001", "broken"),
            FakeEltdxRow("000002", "limit_down"),
        ]


class FakeEltdxClient:
    def limit_up_down_list(self, date):
        return FakeEltdxResult()


class FailedEltdxClient:
    def limit_up_down_list(self, date):
        raise TimeoutError("eltdx unavailable")


class ShortLineForwardTest(unittest.TestCase):

    def test_forward_policy_is_strict_and_permanently_shadow_only(self):
        policy = load_shortline_forward_policy(
            Path(__file__).resolve().parents[1] /
            "configs/selection/shortline_forward_v1.json")
        self.assertEqual(policy.nominal_start_date, 20261009)
        self.assertEqual(policy.feature_mode, "shadow_only")
        self.assertFalse(policy.order_mutation_allowed)
        self.assertEqual(set(policy.required_datasets), set(DATASETS))

    def test_audit_ignores_legacy_prestart_declaration_but_rejects_new_order_mode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "_runs/20261003/legacy.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({
                "collector_version": "akshare_shortline_forward_v1",
                "trade_date": 20261003, "status": "skipped_non_trading_day",
                "feature_mode": "shadow_only",
            }), encoding="utf-8")
            _, legacy_report = audit(root)
            self.assertEqual(legacy_report["status"], "empty")
            governed = root / "_runs/20261009/governed.json"
            governed.parent.mkdir(parents=True)
            governed.write_text(json.dumps({
                "collector_version": "akshare_shortline_forward_v1",
                "trade_date": 20261009, "status": "captured",
                "forward_policy_version": "shortline_forward_v1",
                "feature_mode": "enforced", "order_mutation_allowed": True,
                "forward_sample_eligible": False,
            }), encoding="utf-8")
            _, governed_report = audit(root)
            self.assertEqual(governed_report["status"], "failed")

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

    def test_auction_status_uses_auction_capture_not_close_requirements(self):
        with TemporaryDirectory() as directory:
            result = collect(
                trade_date=20261009, phase="auction", output_dir=directory,
                paper_dir=directory,
                now=datetime(2026, 10, 9, 9, 26, 30, tzinfo=SHANGHAI),
                ak_module=FakeAuctionAkShare(), calendar_dates={20261009})
            self.assertEqual(result["status"], "captured")
            self.assertEqual(result["required_capture_count"], 0)
            self.assertEqual(result["asof_eligible_count"], 1)

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
            self.assertEqual(result["strategy_feature_eligible_count"], 0)
            self.assertEqual(result["feature_mode"], "shadow_only")
            self.assertFalse(result["order_mutation_allowed"])
            self.assertEqual(result["paper_order_effect"], "none")
            self.assertTrue(result["forward_sample_eligible"])
            self.assertEqual(result["forward_anchor_trade_date"], 20261009)
            self.assertEqual(len(result["market_snapshot_evidence"]), 2)
            self.assertEqual(len(list((root / "forward").glob(
                "20261009/*/*/metadata.json"))), len(DATASETS))
            audit_rows, audit_report = audit(root / "forward")
            self.assertEqual(audit_report["status"], "passed")
            self.assertEqual(len(audit_rows), len(DATASETS))
            self.assertEqual(audit_report["forward_anchor_trade_date"], 20261009)
            self.assertEqual(audit_report["forward_eligible_dates"], [20261009])

    def test_eltdx_sidecar_is_normalized_but_never_strategy_eligible(self):
        normalized = normalize_eltdx_limit_events(
            FakeEltdxResult(), 20261009,
            "2026-10-09T15:30:00+08:00")
        self.assertEqual(
            normalized.event_type.tolist(),
            ["CLOSED_UPPER", "FAILED_UPPER_CLOSE", "CLOSED_LOWER"])
        self.assertEqual(normalized.iloc[0].symbol, "sz000001")
        self.assertEqual(normalized.iloc[0].limit_reason_raw, "并购重组")
        self.assertTrue(pd.isna(normalized.iloc[0].selection_reason_raw))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = collect(
                trade_date=20261009, phase="close", output_dir=root,
                paper_dir=root / "paper",
                now=datetime(2026, 10, 9, 15, 30, tzinfo=SHANGHAI),
                ak_module=FakeAkShare(), calendar_dates={20261009},
                enable_eltdx_shadow=True, eltdx_client=FakeEltdxClient())
            self.assertEqual(result["status"], "captured")
            self.assertTrue(result["forward_archive_complete"])
            self.assertEqual(result["required_capture_count"], len(DATASETS))
            self.assertEqual(result["optional_capture_count"], 1)
            self.assertEqual(result["optional_capture_status"], "captured")
            self.assertEqual(result["strategy_feature_eligible_count"], 0)
            self.assertEqual(len(result["cross_source_comparisons"]), 3)
            sidecar = [item for item in result["captures"]
                       if item["dataset"] == ELTDX_DATASET][0]
            self.assertTrue(sidecar["asof_feature_allowed"])
            self.assertFalse(sidecar["strategy_feature_allowed"])
            self.assertIn("SECONDARY_SOURCE_UNVALIDATED",
                          sidecar["quality_codes"])

    def test_eltdx_failure_does_not_block_required_archive_or_anchor(self):
        with TemporaryDirectory() as directory, mock.patch(
                "scripts.collect_shortline_events.time.sleep"):
            root = Path(directory)
            result = collect(
                trade_date=20261009, phase="close", output_dir=root,
                paper_dir=root / "paper",
                now=datetime(2026, 10, 9, 15, 30, tzinfo=SHANGHAI),
                ak_module=FakeAkShare(), calendar_dates={20261009},
                enable_eltdx_shadow=True,
                eltdx_client=FailedEltdxClient())
            self.assertEqual(result["status"], "captured")
            self.assertTrue(result["forward_archive_complete"])
            self.assertTrue(result["forward_sample_eligible"])
            self.assertEqual(result["optional_capture_status"],
                             "captured_partial_or_failed")
            sidecar = [item for item in result["captures"]
                       if item["dataset"] == ELTDX_DATASET][0]
            self.assertEqual(sidecar["status"], "error")
            self.assertFalse(sidecar["asof_feature_allowed"])
            self.assertEqual(result["cross_source_comparisons"], [])

    def test_forward_anchor_waits_for_first_complete_archive_on_or_after_date(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            before = collect(
                trade_date=20261008, phase="close", output_dir=root,
                paper_dir=root / "paper",
                now=datetime(2026, 10, 8, 15, 30, tzinfo=SHANGHAI),
                ak_module=FakeAkShare(), calendar_dates={20261008})
            self.assertEqual(before["forward_sample_status"], "prestart_shadow")
            self.assertFalse(before["forward_sample_eligible"])
            self.assertIsNone(before["forward_anchor_trade_date"])

            incomplete = collect(
                trade_date=20261009, phase="close", output_dir=root,
                paper_dir=root / "paper",
                now=datetime(2026, 10, 9, 15, 30, tzinfo=SHANGHAI),
                ak_module=PartialAkShare(), calendar_dates={20261009})
            self.assertEqual(
                incomplete["forward_sample_status"],
                "awaiting_successful_archive")
            self.assertFalse(incomplete["forward_sample_eligible"])

            first = collect(
                trade_date=20261012, phase="close", output_dir=root,
                paper_dir=root / "paper",
                now=datetime(2026, 10, 12, 15, 30, tzinfo=SHANGHAI),
                ak_module=FakeAkShare(), calendar_dates={20261012})
            self.assertEqual(first["forward_sample_status"],
                             "eligible_forward_shadow")
            self.assertTrue(first["forward_sample_eligible"])
            self.assertEqual(first["forward_anchor_trade_date"], 20261012)

            policy = load_shortline_forward_policy(
                Path(__file__).resolve().parents[1] /
                "configs/selection/shortline_forward_v1.json")
            anchor = read_forward_anchor(root, policy)
            self.assertEqual(anchor["anchor_trade_date"], 20261012)
            _, report = audit(root)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["forward_eligible_dates"], [20261012])

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

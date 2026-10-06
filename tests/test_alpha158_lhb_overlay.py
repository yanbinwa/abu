"""Dragon-Tiger List ingestion and lagged-factor tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuLongHuBangFactors import (
    LHB_FEATURES, build_lhb_features,
)
from scripts.download_akshare_lhb_history_v1 import (
    normalize_detail, normalize_institution,
)
from scripts.validate_alpha158_lhb_overlay_v1 import validate_config


def detail_frame(date="2024-01-02", net=30.0):
    return pd.DataFrame([{
        "代码": "000001", "上榜日": date, "龙虎榜净买额": net,
        "龙虎榜买入额": 80.0, "龙虎榜卖出额": 50.0,
        "龙虎榜成交额": 130.0, "市场总成交额": 1000.0,
        "换手率": 5.0, "流通市值": 1e9, "上榜原因": "测试",
        "解读": "不得入模", "上榜后1日": 99.0,
        "上榜后2日": 99.0, "上榜后5日": 99.0,
        "上榜后10日": 99.0,
    }])


def institution_frame(date="2024-01-02", net=20.0):
    return pd.DataFrame([{
        "代码": "000001", "上榜日期": date, "买方机构数": 3,
        "卖方机构数": 1, "机构买入总额": 50.0,
        "机构卖出总额": 30.0, "机构买入净额": net,
        "市场总成交额": 1000.0, "换手率": 5.0,
        "流通市值": 1e9, "上榜原因": "测试",
    }])


class Alpha158LhbOverlayTest(unittest.TestCase):

    def test_config_freezes_current_event_exit_only_policy(self):
        path = (Path(__file__).resolve().parents[1] /
                "configs/selection/alpha158_lhb_overlay_v1.json")
        config = json.loads(path.read_text(encoding="utf-8"))
        validate_config(config)
        config["review_overlay"] = "ranking_exits_enabled"
        with self.assertRaisesRegex(ValueError, "event-exit-only"):
            validate_config(config)

    def test_normalization_excludes_forward_returns_and_interpretation(self):
        detail = normalize_detail(detail_frame())
        institution = normalize_institution(institution_frame())
        self.assertEqual(detail.trade_date.iloc[0], 20240102)
        self.assertEqual(institution.trade_date.iloc[0], 20240102)
        self.assertFalse(any("上榜后" in column for column in detail.columns))
        self.assertNotIn("解读", detail.columns)

    def test_features_use_previous_session_and_zero_non_events(self):
        calendar = np.asarray([20240102, 20240103, 20240104, 20240105])
        predictions = pd.DataFrame([
            {"signal_asof": date, "symbol": symbol, "ridge_score": score}
            for date in calendar[1:]
            for symbol, score in (("sz000001", 2.0), ("sz000002", 1.0))
        ])
        detail = normalize_detail(detail_frame())
        detail["symbol"] = "sz000001"
        institution = normalize_institution(institution_frame())
        institution["symbol"] = "sz000001"
        result = build_lhb_features(
            predictions, detail, institution, calendar, calendar,
            rolling_sessions=2, availability_lag_sessions=1)
        first = result[(result.signal_asof == 20240103) &
                       result.symbol.eq("sz000001")].iloc[0]
        non_event = result[(result.signal_asof == 20240103) &
                           result.symbol.eq("sz000002")].iloc[0]
        later = result[(result.signal_asof == 20240104) &
                       result.symbol.eq("sz000001")].iloc[0]
        self.assertEqual(first.source_date, 20240102)
        self.assertEqual(first.lhb_event_1d, 1.0)
        self.assertEqual(non_event.lhb_event_1d, 0.0)
        self.assertEqual(later.lhb_event_1d, 0.0)
        self.assertAlmostEqual(first.lhb_event_frequency_20d, .5)
        self.assertAlmostEqual(later.lhb_event_frequency_20d, .5)
        self.assertTrue(np.isfinite(result[list(LHB_FEATURES)]).all().all())

    def test_future_event_does_not_change_prior_feature(self):
        calendar = np.asarray([20240102, 20240103, 20240104])
        predictions = pd.DataFrame([
            {"signal_asof": 20240103, "symbol": "sz000001",
             "ridge_score": 1.0},
        ])
        detail = normalize_detail(detail_frame())
        detail["symbol"] = "sz000001"
        institution = normalize_institution(institution_frame())
        institution["symbol"] = "sz000001"
        before = build_lhb_features(
            predictions, detail, institution, calendar, calendar)
        future_detail = pd.concat([
            detail, normalize_detail(detail_frame("2024-01-04", -100.0))
        ], ignore_index=True)
        future_detail.loc[future_detail.symbol.isna(), "symbol"] = "sz000001"
        after = build_lhb_features(
            predictions, future_detail, institution, calendar, calendar)
        np.testing.assert_allclose(
            before[list(LHB_FEATURES)], after[list(LHB_FEATURES)])

    def test_config_rejects_zero_lag(self):
        config = {
            "history_dataset_version": "akshare_eastmoney_lhb_backfill_v1",
            "research_status": "RETROSPECTIVE_SCREEN_ONLY_NOT_ADMITTED",
            "strict_pit": False, "availability_evidence": "BACKFILLED_QUERY",
            "automatic_admission": False, "parameter_search_allowed": False,
            "availability_lag_sessions": 0, "label_horizon_sessions": 20,
            "model": "median_imputer_standard_scaler_ridge",
            "source_strategy_version": "alpha158_price_only_no_rank_exit_v1",
            "source_policy_version": "alpha158_lite_low_turnover_v3",
            "review_overlay": "suppress_rank_exits",
            "factor_arms": {
                "meta_base": ["base_rank_centered"],
                "meta_lhb": ["base_rank_centered", *LHB_FEATURES],
            },
            "excluded_from_primary": ["post_1d_return"],
        }
        with self.assertRaisesRegex(ValueError, "lag"):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()

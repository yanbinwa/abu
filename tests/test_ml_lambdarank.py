"""M4 LambdaRank relevance, query weights, metrics, and determinism tests."""
import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlpha158Lite import ALPHA158_LITE_FEATURES
from abupy.MLBu.models.ABuLambdaRankPlugin import (
    LambdaRankPlugin, build_ranking_matrix, date_equal_ndcg,
    encode_query_relevance, relevance_from_average_rank,
)


def ranking_frames(seed=19):
    rng = np.random.default_rng(seed)

    def build(dates, sizes):
        rows = []
        for date, size in zip(dates, sizes):
            values = rng.normal(size=(size, len(ALPHA158_LITE_FEATURES)))
            frame = pd.DataFrame(values, columns=ALPHA158_LITE_FEATURES)
            frame.insert(0, "symbol", ["s{:04d}".format(i)
                                        for i in range(size)])
            frame.insert(0, "signal_asof", date)
            target = (0.3*values[:, 0]-0.1*values[:, 3] +
                      rng.normal(scale=.1, size=size))
            frame["target_rank"] = pd.Series(target).rank(
                method="average", pct=True).to_numpy()-0.5
            rows.append(frame)
        return pd.concat(rows, ignore_index=True)
    return build([20250101, 20250102, 20250103], [600, 700, 800]), \
        build([20250201, 20250202], [200, 400])


class LambdaRankPluginTest(unittest.TestCase):

    def test_relevance_boundaries_small_queries_and_ties(self):
        self.assertEqual(relevance_from_average_rank(10.5, 300), 4)
        self.assertEqual(relevance_from_average_rank(20.5, 300), 3)
        self.assertEqual(relevance_from_average_rank(50.5, 300), 2)
        self.assertEqual(relevance_from_average_rank(100.5, 300), 1)
        # Back half wins before fixed Top100 for small queries.
        self.assertEqual(relevance_from_average_rank(60, 100), 0)
        tied = encode_query_relevance(np.ones(100))
        self.assertEqual(set(tied), {0})

    def test_training_weights_equalize_queries_but_validation_is_unweighted(self):
        train, valid = ranking_frames()
        weighted = build_ranking_matrix(train, training_weights=True)
        unweighted = build_ranking_matrix(valid, training_weights=False)
        offset, totals = 0, []
        for size in weighted.groups:
            totals.append(weighted.weights[offset:offset+size].sum())
            offset += size
        np.testing.assert_allclose(totals, np.repeat(totals[0], len(totals)),
                                   rtol=0, atol=1e-12)
        self.assertIsNone(unweighted.weights)

    def test_date_equal_metric_does_not_weight_larger_query_more(self):
        labels = np.r_[np.arange(5, -1, -1), np.tile(np.arange(5, -1, -1), 2)]
        scores = labels.astype(float)
        groups = (6, 12)
        aggregate = date_equal_ndcg(labels, scores, groups, 10)
        first = date_equal_ndcg(labels[:6], scores[:6], (6,), 10)
        second = date_equal_ndcg(labels[6:], scores[6:], (12,), 10)
        self.assertAlmostEqual(aggregate, (first+second)/2, 12)

    def test_fit_audits_native_date_equal_ndcg_and_is_repeatable(self):
        train, valid = ranking_frames()
        first = LambdaRankPlugin().fit(train, valid)
        second = LambdaRankPlugin().fit(train, valid)
        self.assertIsNone(first.query_audit["validation_weights"])
        self.assertAlmostEqual(
            first.evaluation["native_ndcg_at_10"],
            first.evaluation["manual_date_equal_ndcg_at_10"], 12)
        self.assertEqual(first.best_iteration, second.best_iteration)
        self.assertEqual(first.manifest["model_text"],
                         second.manifest["model_text"])
        np.testing.assert_allclose(first.predict(valid), second.predict(valid),
                                   rtol=0, atol=0)


if __name__ == "__main__":
    unittest.main()

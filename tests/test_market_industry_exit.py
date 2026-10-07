import numpy as np

from abupy.AlphaBu.ABuMarketIndustryExit import (
    MarketIndustryExitConfig, _rolling_prior_quantile,
)


def test_rolling_quantile_excludes_current_observation():
    values = np.array([[1.0], [2.0], [100.0], [3.0]])
    result = _rolling_prior_quantile(values, 2, 2, 0.5)[:, 0]
    assert np.isnan(result[0])
    assert np.isnan(result[1])
    assert result[2] == 1.5
    assert result[3] == 51.0


def test_overlay_is_research_only_and_variants_are_explicit():
    for variant in ("market", "industry", "combined"):
        config = MarketIndustryExitConfig(variant=variant)
        assert config.require_trailing_enabled
        assert config.research_only


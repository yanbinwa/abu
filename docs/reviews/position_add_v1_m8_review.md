# Position Add v1 M8 Review

## Scope

M8 completes trade-scoped same-symbol isolation, five-level realized-PnL
attribution, deterministic matched placebo infrastructure with PIT-only matching,
Holm adjustment, and lineage-backed OPEN/INCREASE/REDUCE/CLOSE visualization.

## Verification

- `python -m unittest tests.test_position_add_analytics -v`
- all position-add and legacy project tests
- compileall and `git diff --check`

## Research limits

The placebo runner requires a candidate file with signal-time industry, market
state, holding-age, floating-R and liquidity buckets. It fails closed when a
matched pool is absent; it does not substitute a future-exit-aware pool.

## Gate

The implementation milestone passes only if same-symbol logical exits remain
isolated, all attribution levels reconcile, placebo seeds reproduce exactly,
future exit dates cannot affect matching, and every visual marker maps to a
fill allocation or lot disposition.

## Full-system evidence

- Conservative shared-account VCP + Alpha158 replay: return `+4.1010%`, maximum
  drawdown `-11.0228%`, average exposure `15.41%`, 399 filled buys. This replay
  uses frozen approved signals and does not resurrect source-run rejections.
- Alpha158 TurtleATR exact-PIT placebo: 1,000 paths completed, but only 10 of
  232 actual closed ADDs had a non-actual peer under every required match field
  (`4.31%` coverage). The matched subset ranked at the 100th percentile, which
  is diagnostic only because coverage is too low.
- VCP ProtectedWinner visualization: 188 trades and 428 lineage-backed markers.
- Alpha158 ProtectedWinner visualization: 373 trades and 848 lineage-backed
  markers.

Result: engineering gate passed. Statistical admission remains rejected due to
low placebo coverage and weak or negative executable incremental performance.

Final verification: `python -m unittest discover -s tests -p 'test_*.py' -q`
ran 280 tests with `OK`; compileall and `git diff --check` also passed.

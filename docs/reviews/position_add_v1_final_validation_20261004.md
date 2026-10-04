# Position Add Policy Plugin v1 — Final Validation (2026-10-04)

## Delivery status

M0 through M8 are implemented. The default remains `NoAddPolicy`; no researched
ADD policy is admitted to paper or live trading by this implementation.

## Engineering evidence

- Baseline freeze ID:
  `152a780f6594c0742e430a7f9b467ebd6fa673e3bb8cd75aaf14e6218290aa4b`
- NoAdd golden master: VCP and Alpha158 NAV, orders, reservations, fills and
  risk decisions match all frozen common business fields.
- Full unit/regression suite: 280 tests passed after all implementation,
  documentation and research-run additions.
- Fill allocation, FIFO disposition, fees, cash, lot quantity and realized PnL
  have explicit conservation checks.

## Research conclusions

| Experiment | Return / increment | Drawdown | Decision |
|---|---:|---:|---|
| VCP NoAdd frozen | +1.4363% | -8.7863% | baseline only |
| Alpha158 NoAdd frozen | +3.6005% | -6.9768% | baseline only |
| VCP dynamic-stop NoAdd | -2.2086% | -11.0650% | control |
| VCP ProtectedWinner executable | -2.1127% | -12.5344% | reject |
| Alpha dynamic-stop NoAdd | +4.5459% | -8.3121% | control |
| Alpha ProtectedWinner executable | +3.2358% | -9.5608% | reject |
| VCP Rebreakout executable | -4.7264% | -12.2785% | reject |
| Alpha TurtleATR executable | +3.9516% | -7.8649% | research only |
| Frozen-signal combined account | +4.1010% | -11.0228% | diagnostic |

Fixed-path overlays were often more favorable than executable replay. That
difference confirms cash/risk competition and path dependence are material;
overlay results must not be presented as achievable portfolio returns.

The strict Alpha TurtleATR placebo completed 1,000 paths for a 10-trade matched
subset, but match coverage was only 4.31%. Its high percentile is insufficient
for admission. Broader PIT candidate coverage or a pre-registered coarser match
design is required before statistical claims.

## Artifact locations

- Golden comparisons: `/Users/wjy/abu/backtests/position_add_v1_golden_comparison_20261004`
- ProtectedWinner shadow/executable:
  `/Users/wjy/abu/backtests/position_add_v1_protected_*_20261004`
- Rebreakout and TurtleATR runs:
  `/Users/wjy/abu/backtests/position_add_v1_rebreakout_*_20261004` and
  `/Users/wjy/abu/backtests/position_add_v1_turtle_atr_*_20261004`
- Combined replay: `/Users/wjy/abu/backtests/position_add_v1_combined_20261004`
- Placebo: `/Users/wjy/abu/backtests/position_add_v1_turtle_atr_placebo_20261004`
- Visual review:
  `/Users/wjy/abu/backtests/position_add_v1_trade_visualization_20261004`

## Next gate

Keep every ADD policy disabled by default. Continue only with a new forward
period or a pre-registered experiment that improves PIT placebo coverage without
using observed outcome data to choose matching rules.

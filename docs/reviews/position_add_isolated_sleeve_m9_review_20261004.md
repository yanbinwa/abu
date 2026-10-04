# Position Add M9 — Isolated Sleeve Review (2026-10-04)

## Purpose

M9 tests whether Alpha158 + TurtleATR failed because ADD orders displaced base
entries in the shared account. Total capital is frozen at CNY 1,000,000: CNY
900,000 runs the unchanged Alpha158 base path and CNY 100,000 is either idle
cash or a cash-backed ADD sleeve. TurtleATR parameters are unchanged.

## Implementation and self-test

- `replay_isolated_add_sleeve` applies cash, board-lot, gross exposure,
  next-open, fee, slippage, limit-up and corporate-action rules without
  mutating the base executor.
- `MarketTrendGatePolicy` is a separate plugin version. It fails closed on
  missing fields and uses only signal-date benchmark close and MA200.
- Targeted tests: 18 passed.
- Full regression suite: 299 passed.
- Historical runs completed without ADD fills in the shadow base account.
- Ungated and gated runs produced identical base fills and NAV files
  (`physical_fills.csv` SHA256 `f1412dfe...a8c70f8`, `daily_nav.csv` SHA256
  `6daecddc...84ac9a`), confirming the gate changed only the sleeve.

## Historical diagnostic

| Experiment | Total return | Max drawdown | Average exposure | ADDs | Decision |
|---|---:|---:|---:|---:|---|
| Full-capital dynamic-stop NoAdd reference | +4.5459% | -8.3121% | 17.9958% | 0 | deployment reference |
| 90% base + 10% idle | +2.2192% | -7.9004% | 16.1978% | 0 | matched control |
| TurtleATR isolated sleeve | +5.2312% | -9.3198% | 21.3752% | 232 | drawdown gate failed |
| TurtleATR + market MA200 gate | +4.6177% | -8.3671% | 19.8097% | 159 | research only |

The ungated sleeve earned CNY 30,120.06 and added 3.0120 percentage points to
the partitioned control, but worsened maximum drawdown by 1.4194 percentage
points. The drawdown trough coincided with the base account on 2024-09-18,
showing correlated downside rather than base-order displacement.

The market-gated sleeve earned CNY 23,985.01. It added 2.3985 percentage points
and worsened drawdown by only 0.4667 percentage points, inside the frozen
one-point tolerance. It had 159 closed ADDs, a 54.72% win rate and positive
realized ADD PnL in 2024, 2025 and 2026.

The matched partition comparison isolates sleeve alpha, but it is not the only
deployment decision. Against the existing full-capital dynamic-stop NoAdd
portfolio, the gated candidate improves return by only 0.0718 percentage point
and worsens drawdown by 0.0550 percentage point. Reserving 10% cash changes base
sizing and costs substantial base return, so the present whole-portfolio gain
is economically too thin for admission.

## Interpretation and gate

Cash isolation removes the previously observed opportunity cost. The result is
consistent with TurtleATR containing useful conditional information, while its
losses are correlated with weak broad-market periods. The MA200 gate addresses
that failure mode without changing stock selection or TurtleATR thresholds.

This is not an admission result. The MA200 variant was created after observing
the ungated historical drawdown, and the previously run exact-PIT placebo
matched only 10 of 232 ADDs. Historical data has already been repeatedly
observed. The plugin also has not materially beaten the full-capital NoAdd
deployment reference. It remains disabled by default until a newly registered
forward period supplies at least 30 independent ADD entry clusters and valid
comparisons against both the frozen idle-sleeve control and full-capital NoAdd.

## Artifacts

- Ungated: `/Users/wjy/abu/backtests/position_add_isolated_sleeve_v1_20261004`
- Market gated:
  `/Users/wjy/abu/backtests/position_add_isolated_market_gate_sleeve_v1_20261004`

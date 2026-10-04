# Position Add M10 — Residual Resource Review (2026-10-04)

## Design

The experiment restores the full CNY 1,000,000 Alpha158 account. Base orders,
cash reservations and fills are frozen and always take priority. Market-gated
TurtleATR ADDs may use only residual cash and residual gross, symbol, industry,
portfolio open-risk, same-day risk, liquidity and stress budgets.

ADD quantity is fixed at signal close with the proposal maximum price. The next
open can fill or reject that quantity but cannot resize it from the observed
opening price. The replay records each binding rejection constraint.

## Result

Period: 2023-07-27 through 2026-09-30.

| Metric | Full-capital NoAdd | Residual ADD | Change |
|---|---:|---:|---:|
| Return | +4.5459% | +5.3105% | +0.7646 pp |
| Maximum drawdown | -8.3121% | -8.1397% | +0.1724 pp |
| Average exposure | 17.9958% | 18.3151% | +0.3193 pp |

- Triggered proposals: 168
- Approved and filled ADDs: 20
- Closed ADDs: 20
- Realized ADD PnL: CNY 7,645.93
- Win rate: 65.00%
- Median ADD PnL: CNY 367.88
- Forced exits to protect base cash: 0
- Rejections: stress loss 85, portfolio open risk 36, industry open risk 25

The base strategy path is unchanged. Cash was not the binding resource; the
portfolio's existing risk and stress budgets rejected 146 proposals. The ADD
overlay improved both return and historical drawdown with only 0.32 percentage
point more average exposure.

## Admission decision

The candidate remains `research_only`:

1. Twenty closed ADDs are below the frozen minimum of 30.
2. The three largest gains contribute 76.82% of total ADD PnL.
3. The market gate and residual replay are post-hoc historical diagnostics.
4. A valid matched placebo and new forward period are still absent.

Risk limits must not be relaxed to increase trade count. The appropriate next
evidence is a frozen forward shadow run using the same base-priority and risk
rules.

## Verification and artifacts

- Related tests: 20 passed before the historical run.
- Full regression suite: 323 passed.
- Compile and diff-format checks passed.
- Artifacts: `/Users/wjy/abu/backtests/position_add_residual_v1_20261004`

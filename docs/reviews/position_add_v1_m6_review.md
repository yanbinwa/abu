# Position Add v1 M6 Review

## Scope

M6 implements the frozen ProtectedWinner conditions, fee-aware breakeven,
fixed-path overlay, executable replay, and separate audit outputs.

## Full-sample evidence

| Strategy | Control | ProtectedWinner | ADD sample | Result |
|---|---:|---:|---:|---|
| VCP fixed overlay | base +1.4363% | +1,758.80 cash increment | 44 | small positive |
| Alpha158 fixed overlay | base +3.6005% | -3,015.99 cash increment | 93 | negative |
| VCP executable, dynamic-stop control | -2.2086% | -2.1127% | 52 | +0.096pp; worse drawdown |
| Alpha158 executable, dynamic-stop control | +4.5459% | +3.2358% | 112 | -1.310pp; worse drawdown |

## Verification

- ProtectedWinner boundary and missing-field tests: 4/4
- Overlay isolation tests: 2/2
- Related policy/risk tests: 39/39
- Strategy regressions: 50/50
- Four full-sample replay paths completed with separate artifacts

## Gate

Engineering gate passed. ProtectedWinner is retained as a failed universal
alpha hypothesis: it must remain disabled by default. M7 may implement other
independent policies without tuning ProtectedWinner on these results.

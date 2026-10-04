# Position Add v1 M7 Review

## Scope

M7 adds independently versioned Rebreakout and TurtleATR policies plus
deterministic ALL_OF, ANY_OF and PRIORITY arbitration. Composite constraints
use the strict member intersection and retain member evaluations.

## Verification

- `python -m unittest tests.test_position_add_composite -v`
- all M1-M6 and strategy regression tests
- compileall and `git diff --check`

## Gate

M8 is allowed after prior-window exclusion, ATR threshold, strict merge and
single-order idempotency tests pass.

Unit result: passed on 2026-10-04, 4/4 M7 cases plus 4 inherited
ProtectedWinner boundary cases.

Result: engineering gate passed. The policies remain disabled by default until
their pre-registered full-sample and forward tests pass.

## Full-sample frozen results

| Policy / selection strategy | Fixed overlay | Executable | Matched NoAdd control |
|---|---:|---:|---:|
| Rebreakout / VCP | +17,873.79 cash, 82 ADDs | -4.7264%, MDD -12.2785%, 89 ADDs | -2.2086%, MDD -11.0650% |
| Rebreakout / Alpha158 | +389.44 cash, 119 ADDs | not promoted | +4.5459% dynamic-stop control |
| TurtleATR / VCP | +9,909.40 cash, 86 ADDs | not promoted | -2.2086% dynamic-stop control |
| TurtleATR / Alpha158 | +31,782.48 realized cash, 232 closed ADDs | +3.9516%, MDD -7.8649%, 235 ADDs | +4.5459%, MDD -8.3121% |

The VCP Rebreakout fixed-path gain did not survive executable replay. TurtleATR
reduced Alpha158 drawdown slightly but reduced return and increased exposure.
Neither policy is enabled by default; no parameters were changed after seeing
these results.

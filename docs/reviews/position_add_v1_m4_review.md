# Position Add v1 M4 Review

## Scope

M4 applies symbol headroom, ADD notional and frozen trade-risk caps; settles
ADD risk reservations; aggregates risk by logical trade; and emits deterministic
trade-scoped post-fill de-risk intents.

## Verification

- `python -m unittest tests.test_position_add_risk -v`
- all M1-M3 plus executor/risk regression tests
- compileall and `git diff --check`

## Gate

M5 is allowed only if headroom, idempotent reservations, release paths and
trade-scoped de-risk behavior pass without regressing the legacy suite.

Result: passed on 2026-10-04. New ADD-risk tests: 4/4; M1-M3 and legacy
regressions: 36/36; compile and whitespace checks passed. M5 is allowed.

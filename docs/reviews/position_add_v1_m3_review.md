# Position Add v1 M3 Review

## Scope

M3 adds deterministic logical-trade transitions, lot-level T+1 sell
reservations, exit precedence checks, and deterministic stock-action allocation.

## Verification

- `python -m unittest tests.test_position_lifecycle -v`
- `python -m unittest tests.test_position_ledger_accounting tests.test_portfolio_executor tests.test_portfolio_risk -q`
- compileall and `git diff --check`

## Gate

M4 is allowed only after lifecycle, T+1, exit/add conflict, corporate-action
and all legacy regression tests pass.

Result: passed on 2026-10-04. New lifecycle tests: 4/4; related regressions:
28/28; compile and whitespace checks passed. M4 is allowed.

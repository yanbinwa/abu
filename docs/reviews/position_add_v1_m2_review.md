# Position Add v1 M2 Review

## Scope

M2 connects physical fills to one-to-one logical fill allocations and immutable
position lots. It applies FIFO dispositions for trade-scoped partial exits,
allocates sell fees deterministically, and checks quantity/fee conservation.

## Verification

- `python -m unittest tests.test_position_ledger_accounting -v`
- `python -m unittest tests.test_portfolio_executor tests.test_portfolio_risk -q`
- `python -m compileall abupy/AlphaBu/ABuPositionLedger.py abupy/AlphaBu/ABuPortfolioExecutor.py`
- `git diff --check`

## Gate

M3 is allowed only if OPEN/INCREASE lots, FIFO partial exits, independent
minimum commissions and all existing executor/risk regressions pass.

Result: passed on 2026-10-04. New accounting tests: 3/3; executor/risk
regressions: 25/25; compile and whitespace checks passed. M3 is allowed.

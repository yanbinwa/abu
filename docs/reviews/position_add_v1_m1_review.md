# Position Add v1 M1 Review

## Scope

M1 introduces the immutable three-level position schemas, explicit lineage on
intent/order/fill records, deterministic business IDs, and a legacy position
migration adapter. It does not change execution accounting yet.

## Verification

- `python -m unittest tests.test_position_ledger -v`
- `python -m unittest tests.test_portfolio_executor tests.test_portfolio_risk -q`
- `python -m compileall abupy/AlphaBu/ABuTradeIntent.py abupy/AlphaBu/ABuPositionLedger.py`
- `git diff --check`

## Gate

Proceed to M2 only when schema invariants, ID stability, legacy migration and
existing executor/risk regressions all pass.

Result: passed on 2026-10-04. New tests: 4/4; executor/risk regressions:
25/25; compile and whitespace checks passed. M2 is allowed.

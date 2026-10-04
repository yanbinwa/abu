# Position Add v1 M5 Review

## Scope

M5 adds the immutable policy context/evaluation/proposal contract, idempotent
policy runner, strict configuration loading, and `NoAddPolicy`.

## Verification

- `python -m unittest tests.test_position_add_policy -v`
- full position-ledger/executor/risk regression suite
- VCP and Alpha158 frozen baseline replay plus normalized golden comparison
- compileall and `git diff --check`

## Gate

ProtectedWinner implementation is allowed only after NoAdd generates no order,
reservation or cash change and both frozen strategies pass the golden contract.

Result: passed on 2026-10-04. VCP matched 1,150 NAV rows, 284 fills,
283 orders/reservations and 530 risk decisions. Alpha158 matched 772 NAV rows,
705 fills/orders/reservations and 1,748 risk decisions. All common business
fields were equal under the frozen tolerance. M6 is allowed.

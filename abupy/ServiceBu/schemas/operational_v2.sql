CREATE TABLE IF NOT EXISTS account_receivables (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    receivable_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_id TEXT,
    receivable_type TEXT NOT NULL CHECK (receivable_type IN ('CASH', 'SHARE')),
    cash_micros INTEGER NOT NULL DEFAULT 0 CHECK (cash_micros >= 0),
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    due_session INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPLIED', 'CANCELLED')),
    source_event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY (account_id, receivable_id),
    FOREIGN KEY (account_id, trade_id) REFERENCES logical_trades(account_id, trade_id),
    CHECK (
        (receivable_type='CASH' AND cash_micros > 0 AND quantity = 0) OR
        (receivable_type='SHARE' AND quantity > 0 AND cash_micros = 0
            AND trade_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS sell_reservations (
    account_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reserved_quantity INTEGER NOT NULL CHECK (reserved_quantity > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED', 'CONSUMED', 'EXPIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, reservation_id),
    FOREIGN KEY (account_id, order_id) REFERENCES orders(account_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_account_receivables_due
    ON account_receivables(account_id, due_session, status);

CREATE INDEX IF NOT EXISTS idx_sell_reservations_active
    ON sell_reservations(account_id, symbol, status);

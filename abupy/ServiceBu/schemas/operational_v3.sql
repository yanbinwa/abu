CREATE TABLE IF NOT EXISTS account_daily_closes (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    trading_session INTEGER NOT NULL,
    source_snapshot_id TEXT NOT NULL REFERENCES market_snapshots(snapshot_id),
    cash_micros INTEGER NOT NULL CHECK (cash_micros >= 0),
    reserved_cash_micros INTEGER NOT NULL CHECK (reserved_cash_micros >= 0),
    available_cash_micros INTEGER NOT NULL CHECK (available_cash_micros >= 0),
    position_cost_micros INTEGER NOT NULL CHECK (position_cost_micros >= 0),
    market_value_micros INTEGER NOT NULL CHECK (market_value_micros >= 0),
    unrealized_pnl_micros INTEGER NOT NULL,
    equity_micros INTEGER NOT NULL CHECK (equity_micros >= 0),
    position_count INTEGER NOT NULL CHECK (position_count >= 0),
    closing_marks_sha256 TEXT NOT NULL CHECK (length(closing_marks_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, trading_session)
);

CREATE INDEX IF NOT EXISTS idx_account_daily_closes_snapshot
    ON account_daily_closes(source_snapshot_id);

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64)
);

CREATE TABLE IF NOT EXISTS service_instances (
    service_instance_id TEXT PRIMARY KEY,
    host_name TEXT NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    stop_reason TEXT,
    repository_commit TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64)
);

CREATE TABLE IF NOT EXISTS job_definitions (
    job_id TEXT PRIMARY KEY,
    job_version TEXT NOT NULL,
    category TEXT NOT NULL,
    schedule_json TEXT NOT NULL CHECK (json_valid(schedule_json)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    critical INTEGER NOT NULL CHECK (critical IN (0, 1)),
    UNIQUE (job_id, job_version)
);

CREATE TABLE IF NOT EXISTS job_runs (
    job_run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES job_definitions(job_id),
    job_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'SCHEDULED', 'RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILED',
        'TERMINAL_FAILED', 'SKIPPED')),
    dependency_snapshot_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(dependency_snapshot_ids_json)),
    output_snapshot_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(output_snapshot_ids_json)),
    successful_attempt_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS job_run_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_run_id TEXT NOT NULL REFERENCES job_runs(job_run_id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    service_instance_id TEXT NOT NULL REFERENCES service_instances(service_instance_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILED', 'TERMINAL_FAILED', 'INTERRUPTED')),
    error_code TEXT,
    error_detail TEXT,
    UNIQUE (job_run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS job_ownerships (
    ownership_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    job_category TEXT NOT NULL,
    old_owner TEXT,
    new_owner TEXT NOT NULL,
    writes_market_data INTEGER NOT NULL CHECK (writes_market_data IN (0, 1)),
    writes_account INTEGER NOT NULL CHECK (writes_account IN (0, 1)),
    writes_notification INTEGER NOT NULL CHECK (writes_notification IN (0, 1)),
    shadow_until TEXT,
    cutover_session INTEGER,
    new_owner_enabled_at TEXT,
    old_schedule_disabled_at TEXT,
    disable_evidence_json TEXT CHECK (
        disable_evidence_json IS NULL OR json_valid(disable_evidence_json)),
    rollback_boundary TEXT NOT NULL CHECK (rollback_boundary IN (
        'BEFORE_NEW_WRITE', 'FORWARD_ONLY_AFTER_NEW_WRITE', 'NOT_APPLICABLE')),
    effective_from TEXT NOT NULL,
    UNIQUE (job_id, effective_from)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    snapshot_type TEXT NOT NULL CHECK (snapshot_type IN (
        'DAILY', 'PREOPEN', 'MINUTE', 'FACTOR', 'WATCHLIST')),
    stream_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    previous_snapshot_id TEXT REFERENCES market_snapshots(snapshot_id),
    trading_session INTEGER NOT NULL,
    decision_cutoff TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('BUILDING', 'COMMITTED', 'CORRUPT')),
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    quality_codes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(quality_codes_json)),
    UNIQUE (stream_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS market_snapshot_partitions (
    snapshot_id TEXT NOT NULL REFERENCES market_snapshots(snapshot_id),
    partition_key TEXT NOT NULL,
    partition_manifest_path TEXT NOT NULL,
    partition_manifest_sha256 TEXT NOT NULL CHECK (length(partition_manifest_sha256) = 64),
    terminal_status TEXT NOT NULL,
    latest_available_at TEXT,
    PRIMARY KEY (snapshot_id, partition_key)
);

CREATE TABLE IF NOT EXISTS minute_snapshot_selections (
    snapshot_id TEXT NOT NULL REFERENCES market_snapshots(snapshot_id),
    symbol TEXT NOT NULL,
    business_bar_key TEXT NOT NULL,
    selected_event_id TEXT NOT NULL,
    selected_revision INTEGER NOT NULL CHECK (selected_revision > 0),
    available_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, symbol, business_bar_key)
);

CREATE TABLE IF NOT EXISTS domain_events (
    event_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    previous_event_id TEXT REFERENCES domain_events(event_id),
    event_type TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    trading_session INTEGER,
    source_service TEXT NOT NULL,
    snapshot_id TEXT REFERENCES market_snapshots(snapshot_id),
    account_id TEXT,
    actor_strategy_instance_id TEXT,
    actor_activation_id TEXT,
    affected_trade_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(affected_trade_ids_json)),
    affected_management_activation_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(affected_management_activation_ids_json)),
    causation_id TEXT,
    correlation_id TEXT,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    source_transaction_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (stream_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS event_consumers (
    consumer_id TEXT PRIMARY KEY,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    effective_from_stream TEXT NOT NULL,
    effective_from_sequence INTEGER NOT NULL CHECK (effective_from_sequence > 0),
    retired_at TEXT,
    retired_after_sequence INTEGER,
    retention_class TEXT NOT NULL CHECK (retention_class IN (
        'PERMANENT_AUDIT', 'ARCHIVE_AFTER_BACKUP', 'REBUILDABLE')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_consumptions (
    consumer_id TEXT NOT NULL REFERENCES event_consumers(consumer_id),
    event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    consumed_at TEXT NOT NULL,
    result_sha256 TEXT CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    PRIMARY KEY (consumer_id, event_id)
);

CREATE TABLE IF NOT EXISTS stream_watermarks (
    consumer_id TEXT NOT NULL REFERENCES event_consumers(consumer_id),
    stream_id TEXT NOT NULL,
    last_consumed_sequence INTEGER NOT NULL CHECK (last_consumed_sequence >= 0),
    state TEXT NOT NULL CHECK (state IN ('READY', 'WAITING_FOR_GAP', 'BLOCKED', 'RETIRED')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (consumer_id, stream_id)
);

CREATE TABLE IF NOT EXISTS strategy_instances (
    strategy_instance_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL UNIQUE,
    account_version INTEGER NOT NULL DEFAULT 0 CHECK (account_version >= 0),
    strategy_instance_id TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    active_activation_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('SHADOW', 'PAPER', 'PAUSED', 'BLOCKED', 'RETIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_activations (
    activation_id TEXT PRIMARY KEY,
    strategy_instance_id TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    effective_from_session INTEGER NOT NULL,
    effective_until_session INTEGER,
    previous_activation_id TEXT REFERENCES strategy_activations(activation_id),
    change_reason TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    UNIQUE (strategy_instance_id, effective_from_session)
);

CREATE TABLE IF NOT EXISTS account_sessions (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    trading_session INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'CREATED', 'PREOPEN_INPUTS_READY', 'RECEIVABLES_APPLIED',
        'OPEN_SELLS_PROCESSED', 'INTRADAY_BUYS_ENABLED', 'DAILY_CLOSE_COMPLETED')),
    preopen_snapshot_id TEXT REFERENCES market_snapshots(snapshot_id),
    blocked_reason TEXT,
    phase_version INTEGER NOT NULL DEFAULT 0 CHECK (phase_version >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, trading_session)
);

CREATE TABLE IF NOT EXISTS account_balances (
    account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
    cash_micros INTEGER NOT NULL,
    reserved_cash_micros INTEGER NOT NULL CHECK (reserved_cash_micros >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_events (
    account_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    account_version INTEGER NOT NULL CHECK (account_version > 0),
    event_type TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, account_version),
    UNIQUE (account_id, event_id)
);

CREATE TABLE IF NOT EXISTS processed_events (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    processed_at TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    PRIMARY KEY (account_id, event_id)
);

CREATE TABLE IF NOT EXISTS logical_trades (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    trade_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_under_activation_id TEXT NOT NULL REFERENCES strategy_activations(activation_id),
    management_activation_id TEXT NOT NULL REFERENCES strategy_activations(activation_id),
    entry_policy_id TEXT NOT NULL,
    entry_policy_version TEXT NOT NULL,
    exit_policy_id TEXT NOT NULL,
    exit_policy_version TEXT NOT NULL,
    risk_policy_id TEXT NOT NULL,
    risk_policy_version TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    PRIMARY KEY (account_id, trade_id)
);

CREATE TABLE IF NOT EXISTS trade_management_assignments (
    assignment_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    from_activation_id TEXT REFERENCES strategy_activations(activation_id),
    to_activation_id TEXT NOT NULL REFERENCES strategy_activations(activation_id),
    exit_policy_id TEXT NOT NULL,
    exit_policy_version TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    reason TEXT NOT NULL,
    takeover_event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    FOREIGN KEY (account_id, trade_id) REFERENCES logical_trades(account_id, trade_id),
    UNIQUE (account_id, trade_id, effective_from)
);

CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    sellable_quantity INTEGER NOT NULL CHECK (sellable_quantity >= 0),
    average_cost_micros INTEGER NOT NULL CHECK (average_cost_micros >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS position_lots (
    lot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    sellable_on_session INTEGER NOT NULL,
    cost_price_micros INTEGER NOT NULL CHECK (cost_price_micros > 0),
    opened_at TEXT NOT NULL,
    FOREIGN KEY (account_id, trade_id) REFERENCES logical_trades(account_id, trade_id)
);

CREATE TABLE IF NOT EXISTS orders (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    order_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    strategy_instance_id TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    actor_activation_id TEXT REFERENCES strategy_activations(activation_id),
    trade_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL CHECK (status IN (
        'APPROVED', 'WAITING', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED')),
    valid_session INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id)
);

CREATE TABLE IF NOT EXISTS order_execution_states (
    account_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    execution_policy_id TEXT NOT NULL,
    state TEXT NOT NULL,
    last_consumed_minute_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (last_consumed_minute_sequence >= 0),
    last_transition_event_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id),
    FOREIGN KEY (account_id, order_id) REFERENCES orders(account_id, order_id)
);

CREATE TABLE IF NOT EXISTS reservations (
    account_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    reserved_cash_micros INTEGER NOT NULL CHECK (reserved_cash_micros >= 0),
    reserved_risk_micros INTEGER NOT NULL CHECK (reserved_risk_micros >= 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED', 'CONSUMED', 'EXPIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, reservation_id),
    FOREIGN KEY (account_id, order_id) REFERENCES orders(account_id, order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    account_id TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    reference_price_micros INTEGER NOT NULL CHECK (reference_price_micros > 0),
    fill_price_micros INTEGER NOT NULL CHECK (fill_price_micros > 0),
    fees_micros INTEGER NOT NULL CHECK (fees_micros >= 0),
    source_snapshot_id TEXT REFERENCES market_snapshots(snapshot_id),
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (account_id, fill_id),
    FOREIGN KEY (account_id, order_id) REFERENCES orders(account_id, order_id)
);

CREATE TABLE IF NOT EXISTS position_events (
    position_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    order_id TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('APPROVE', 'RESIZE', 'REJECT')),
    reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    source_snapshot_id TEXT REFERENCES market_snapshots(snapshot_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    notification_event_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES domain_events(event_id),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'IN_PROGRESS', 'SENT', 'REQUIRES_ATTENTION', 'ABANDONED_BY_OPERATOR')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, notification_event_id)
);

CREATE TABLE IF NOT EXISTS notification_parts (
    notification_event_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    part_kind TEXT NOT NULL CHECK (part_kind IN ('TEXT', 'CHART_IMAGE')),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RENDERED', 'RETRY_PENDING', 'SENDING', 'SENT', 'UNKNOWN',
        'RETRYABLE_FAILED', 'REQUIRES_ATTENTION', 'ABANDONED_BY_OPERATOR')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    remote_reference TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (notification_event_id, part_kind),
    FOREIGN KEY (account_id, notification_event_id)
        REFERENCES notification_outbox(account_id, notification_event_id)
);

CREATE TABLE IF NOT EXISTS render_artifacts (
    artifact_id TEXT PRIMARY KEY,
    notification_event_id TEXT,
    artifact_kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    reconciliation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    trading_session INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PASSED', 'FAILED')),
    findings_json TEXT NOT NULL CHECK (json_valid(findings_json)),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (account_id, trading_session)
);

CREATE TABLE IF NOT EXISTS audit_findings (
    finding_id TEXT PRIMARY KEY,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    category TEXT NOT NULL,
    account_id TEXT,
    snapshot_id TEXT,
    event_id TEXT,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS account_cutovers (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    cutover_epoch INTEGER NOT NULL CHECK (cutover_epoch > 0),
    old_owner TEXT NOT NULL,
    new_owner TEXT NOT NULL,
    frozen_old_state_sha256 TEXT NOT NULL CHECK (length(frozen_old_state_sha256) = 64),
    new_database_backup_id TEXT NOT NULL,
    old_schedule_disabled_evidence_json TEXT NOT NULL
        CHECK (json_valid(old_schedule_disabled_evidence_json)),
    first_new_event_id TEXT,
    first_new_account_version INTEGER,
    cutover_marker_path TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, cutover_epoch)
);

CREATE INDEX IF NOT EXISTS idx_domain_events_unordered
    ON domain_events(stream_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_job_attempts_run
    ON job_run_attempts(job_run_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_orders_session
    ON orders(account_id, valid_session, status);
CREATE INDEX IF NOT EXISTS idx_notifications_status
    ON notification_parts(status, next_attempt_at);

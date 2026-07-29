-- TimescaleDB schema for warm F&O tick / greeks storage.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS fno_ticks (
    time        TIMESTAMPTZ         NOT NULL,
    token       BIGINT              NOT NULL,
    last_price  DOUBLE PRECISION,
    volume      BIGINT,
    oi          BIGINT,
    iv          DOUBLE PRECISION,
    delta       DOUBLE PRECISION
);

-- Convert to a hypertable partitioned on time (idempotent on re-run).
SELECT create_hypertable(
    'fno_ticks',
    'time',
    if_not_exists => TRUE
);

-- Primary access pattern: latest / range queries per instrument token.
CREATE INDEX IF NOT EXISTS fno_ticks_token_time_idx
    ON fno_ticks (token, time DESC);

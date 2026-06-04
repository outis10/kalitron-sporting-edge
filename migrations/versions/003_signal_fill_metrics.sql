-- ============================================================
-- Migration 003 — Signal fill metrics & CLV columns
-- Run: psql $DATABASE_URL -f migrations/versions/003_signal_fill_metrics.sql
-- ============================================================

BEGIN;

-- ── signals: CLOB prices at detection time ───────────────────────────────────
ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS clob_bid            FLOAT,
    ADD COLUMN IF NOT EXISTS clob_ask            FLOAT,
    ADD COLUMN IF NOT EXISTS estimated_fill_price FLOAT,
    ADD COLUMN IF NOT EXISTS book_liquidity_usd   FLOAT;

-- ── bets: execution quality + CLV tracking ───────────────────────────────────
ALTER TABLE bets
    ADD COLUMN IF NOT EXISTS actual_fill_price FLOAT,
    ADD COLUMN IF NOT EXISTS closing_price     FLOAT,
    ADD COLUMN IF NOT EXISTS clv               FLOAT;

-- Index to support CLV tracker job (finds bets near kickoff quickly)
CREATE INDEX IF NOT EXISTS idx_bets_status_kickoff
    ON bets (status, kickoff_utc)
    WHERE status IN ('open', 'paper');

COMMIT;

-- ============================================================
-- Migration 002 — Position Manager & Bet Settler columns
-- Run: psql $DATABASE_URL -f migrations/versions/002_position_manager.sql
-- ============================================================

BEGIN;

-- ── bets: new columns for pre-match position management ──────────────────────

-- Token ID of the shares we hold — needed to place SELL order on close
ALTER TABLE bets
    ADD COLUMN IF NOT EXISTS token_id      VARCHAR(128),
    ADD COLUMN IF NOT EXISTS kickoff_utc   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS close_price   FLOAT,
    ADD COLUMN IF NOT EXISTS close_reason  VARCHAR(40);

-- Widen status to accommodate "closed" alongside existing values
ALTER TABLE bets
    ALTER COLUMN status TYPE VARCHAR(20);

CREATE INDEX IF NOT EXISTS idx_bets_token    ON bets (token_id);
CREATE INDEX IF NOT EXISTS idx_bets_kickoff  ON bets (kickoff_utc);

-- ── matches: lineups flag (already have lineups_available but add lineup cols) -

ALTER TABLE matches
    ADD COLUMN IF NOT EXISTS home_lineup JSONB,
    ADD COLUMN IF NOT EXISTS away_lineup JSONB;

COMMIT;

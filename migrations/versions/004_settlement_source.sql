-- ============================================================
-- Migration 004 — Settlement source tracking
-- Run: psql $DATABASE_URL -f migrations/versions/004_settlement_source.sql
-- ============================================================

BEGIN;

-- Track which data source confirmed the settlement result.
-- Values: 'api_football' | 'polymarket' | 'both'
-- NULL means the bet has not been settled yet.
ALTER TABLE bets
    ADD COLUMN IF NOT EXISTS settlement_source VARCHAR(20);

COMMIT;

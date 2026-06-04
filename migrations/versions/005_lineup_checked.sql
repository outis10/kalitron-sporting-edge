-- ============================================================
-- Migration 005 — lineup_checked flag on bets
-- Run: psql $DATABASE_URL -f migrations/versions/005_lineup_checked.sql
-- ============================================================

BEGIN;

-- Tracks whether the lineup-check stage (stage 1 pre-kickoff close) has run.
-- Default FALSE: existing open bets will be checked on the next position_manager cycle.
ALTER TABLE bets
    ADD COLUMN IF NOT EXISTS lineup_checked BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;

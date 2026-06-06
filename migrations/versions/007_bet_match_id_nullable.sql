-- Migration 007: make match_id nullable in bets for outright (non-match) bets
-- Outright WC bets have no associated match row — match_id must be optional.
BEGIN;

ALTER TABLE bets
    DROP CONSTRAINT IF EXISTS bets_match_id_fkey;

ALTER TABLE bets
    ALTER COLUMN match_id DROP NOT NULL;

ALTER TABLE bets
    ALTER COLUMN match_id TYPE VARCHAR(128);

COMMIT;

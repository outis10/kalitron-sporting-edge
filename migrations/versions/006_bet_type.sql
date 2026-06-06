-- Migration 006: add bet_type to bets table
-- Differentiates match bets (1X2) from outright tournament bets
ALTER TABLE bets ADD COLUMN IF NOT EXISTS bet_type VARCHAR(20) NOT NULL DEFAULT 'match';

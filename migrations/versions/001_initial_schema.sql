-- ============================================================
-- Migration 001 — Initial Schema
-- Run: psql $DATABASE_URL -f migrations/versions/001_initial_schema.sql
-- ============================================================

BEGIN;

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- for LIKE index on market questions

-- ── Leagues ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leagues (
    id      INTEGER PRIMARY KEY,          -- API-Football league ID
    name    VARCHAR(120) NOT NULL,
    country VARCHAR(80)  NOT NULL,
    season  INTEGER      NOT NULL,
    active  BOOLEAN      NOT NULL DEFAULT TRUE
);

-- ── Teams ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS teams (
    id       INTEGER PRIMARY KEY,         -- API-Football team ID
    name     VARCHAR(120) NOT NULL,
    country  VARCHAR(80)  NOT NULL DEFAULT '',
    logo_url VARCHAR(500)
);

-- ── Matches ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS matches (
    id                  VARCHAR(32)  PRIMARY KEY,  -- fixture ID
    league_id           INTEGER      NOT NULL REFERENCES leagues(id),
    home_team_id        INTEGER      NOT NULL REFERENCES teams(id),
    away_team_id        INTEGER      NOT NULL REFERENCES teams(id),
    kickoff_utc         TIMESTAMPTZ  NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'scheduled',
    venue               VARCHAR(200),

    -- Result
    home_goals          INTEGER,
    away_goals          INTEGER,
    result_outcome      VARCHAR(10),        -- 'home' | 'draw' | 'away'

    -- Enrichment flags
    lineups_available   BOOLEAN      NOT NULL DEFAULT FALSE,
    injuries_fetched    BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Raw payload
    raw_fixture_json    JSONB,

    created_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_matches_kickoff ON matches (kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_matches_status  ON matches (status);
CREATE INDEX IF NOT EXISTS idx_matches_league  ON matches (league_id);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_matches_updated_at ON matches;
CREATE TRIGGER trg_matches_updated_at
    BEFORE UPDATE ON matches
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Market Odds Snapshots ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_odds (
    id               UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    match_id         VARCHAR(32)  NOT NULL REFERENCES matches(id),
    condition_id     VARCHAR(128) NOT NULL,
    market_question  TEXT         NOT NULL,
    outcome          VARCHAR(10)  NOT NULL,  -- 'home' | 'draw' | 'away'

    yes_price        FLOAT        NOT NULL,
    no_price         FLOAT        NOT NULL,
    volume_24h       FLOAT        NOT NULL DEFAULT 0,
    liquidity        FLOAT        NOT NULL DEFAULT 0,
    yes_token_id     VARCHAR(128),
    no_token_id      VARCHAR(128),

    fetched_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_market_snapshot UNIQUE (condition_id, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_market_odds_condition ON market_odds (condition_id);
CREATE INDEX IF NOT EXISTS idx_market_odds_match     ON market_odds (match_id);
CREATE INDEX IF NOT EXISTS idx_market_odds_fetched   ON market_odds (fetched_at DESC);

-- ── Predictions ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id             UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    match_id       VARCHAR(32) NOT NULL REFERENCES matches(id),
    model_version  VARCHAR(50) NOT NULL,

    prob_home      FLOAT       NOT NULL,
    prob_draw      FLOAT       NOT NULL,
    prob_away      FLOAT       NOT NULL,
    confidence     FLOAT       NOT NULL,

    factors_used   JSONB,
    reasoning      TEXT,

    predicted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_predictions_match ON predictions (match_id);

-- ── Signals ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    match_id            VARCHAR(32) NOT NULL REFERENCES matches(id),
    condition_id        VARCHAR(128) NOT NULL,
    target_outcome      VARCHAR(10) NOT NULL,
    bet_side            VARCHAR(5)  NOT NULL,   -- 'YES' | 'NO'

    model_probability   FLOAT       NOT NULL,
    market_probability  FLOAT       NOT NULL,
    expected_value      FLOAT       NOT NULL,
    edge                FLOAT       NOT NULL,
    signal_strength     VARCHAR(10) NOT NULL,   -- 'strong' | 'moderate' | 'weak'

    acted_on            BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_match     ON signals (match_id);
CREATE INDEX IF NOT EXISTS idx_signals_created   ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_acted     ON signals (acted_on);

-- ── Bets ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bets (
    id                   UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id            UUID        NOT NULL,
    match_id             VARCHAR(32) NOT NULL REFERENCES matches(id),
    condition_id         VARCHAR(128) NOT NULL,
    market_question      TEXT        NOT NULL,
    outcome              VARCHAR(10) NOT NULL,
    side                 VARCHAR(5)  NOT NULL,

    entry_price          FLOAT       NOT NULL,
    size_usd             FLOAT       NOT NULL,
    shares               FLOAT       NOT NULL,

    paper_trade          BOOLEAN     NOT NULL DEFAULT TRUE,
    status               VARCHAR(10) NOT NULL DEFAULT 'pending',
    polymarket_order_id  VARCHAR(128),

    placed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at           TIMESTAMPTZ,
    pnl_usd              FLOAT,
    settlement_price     FLOAT
);

CREATE INDEX IF NOT EXISTS idx_bets_match      ON bets (match_id);
CREATE INDEX IF NOT EXISTS idx_bets_status     ON bets (status);
CREATE INDEX IF NOT EXISTS idx_bets_paper      ON bets (paper_trade);
CREATE INDEX IF NOT EXISTS idx_bets_placed     ON bets (placed_at DESC);

-- ── Daily Performance ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_performance (
    id               SERIAL  PRIMARY KEY,
    date             DATE    NOT NULL,
    is_paper         BOOLEAN NOT NULL DEFAULT TRUE,

    bets_placed      INTEGER NOT NULL DEFAULT 0,
    bets_won         INTEGER NOT NULL DEFAULT 0,
    bets_lost        INTEGER NOT NULL DEFAULT 0,
    gross_pnl_usd    FLOAT   NOT NULL DEFAULT 0,
    bankroll_end_usd FLOAT   NOT NULL DEFAULT 0,
    roi_pct          FLOAT   NOT NULL DEFAULT 0,
    model_accuracy   FLOAT,
    avg_ev_signalled FLOAT,

    CONSTRAINT uq_daily_perf UNIQUE (date, is_paper)
);

COMMIT;

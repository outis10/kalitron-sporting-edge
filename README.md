# Kalitron Sporting Edge

Multi-agent AI system for finding value bets in football prediction markets on [Polymarket](https://polymarket.com).

## How it works

The system runs a LangGraph pipeline that:

1. **Collects** upcoming fixtures from API-Football and finds the corresponding Polymarket markets
2. **Predicts** match probabilities using a Dixon-Coles inspired Poisson model (pure NumPy/SciPy, no black-box ML)
3. **Analyzes** odds — if `EV = (p_model / p_market) - 1 >= 5%`, a signal is generated
4. **Sizes** the position via Quarter Kelly Criterion (capped at 2% of bankroll)
5. **Executes** FAK orders on the Polymarket CLOB (paper trading by default)
6. **Monitors** open positions every 5 minutes (take-profit / stop-loss / force-close before kickoff)
7. **Settles** bets once the match result is confirmed
8. **Reports** signals and daily P&L via Telegram

```
DataCollector → ModelPredictor → OddsAnalyzer → RiskManager → ExecutionAgent → ReportAgent
```

## Leagues monitored

| ID | League | Reason |
|----|--------|--------|
| 39 | Premier League | Highest liquidity on Polymarket |
| 140 | La Liga | Strong liquidity, efficient data coverage |
| 2 | Champions League | High volume, cross-league validation |

## Stack

| Layer | Technology |
|---|---|
| Orchestration | LangGraph + LangChain |
| LLM | Anthropic Claude Sonnet 4.6 |
| Prediction model | Dixon-Coles Poisson (NumPy/SciPy) |
| Football data | API-Football v3 |
| Prediction markets | Polymarket CLOB + Gamma API |
| API server | FastAPI + APScheduler |
| Database | PostgreSQL 16 (SQLAlchemy asyncio + Alembic) |
| Notifications | Telegram Bot |

## Quickstart

### 1. Prerequisites

- Python 3.11+
- Docker + Docker Compose
- API-Football key (free tier available)
- Anthropic API key

### 2. Setup

```bash
git clone <repo>
cd kalitron-sporting-edge
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and API_FOOTBALL_KEY at minimum
./setup.sh
```

### 3. Run with Docker

```bash
docker compose up -d
```

### 4. Run pipeline manually

```bash
# Single pipeline run
sporting-edge run

# Start monitoring API server (port 8001)
sporting-edge serve

# Run backtest on historical data
sporting-edge backtest data/sample_backtest.csv
```

## Configuration

All settings are in `.env`. Key parameters:

```bash
# Safety — keep true until model is validated
PAPER_TRADING=true
EXECUTE_TRADES=false

# Risk
BANKROLL_USD=1000.0
MAX_KELLY_FRACTION=0.25              # Quarter Kelly
MAX_BET_PCT_BANKROLL=0.02            # Hard cap: 2% per bet
MIN_EV_THRESHOLD=0.05                # Paper trading / backtesting
MIN_EV_THRESHOLD_LIVE=0.08           # Production (absorbs spread + slippage + model error)
MIN_EV_THRESHOLD_LOW_LIQUIDITY=0.12  # Markets with $5k–$10k liquidity in live mode
MIN_MARKET_LIQUIDITY=5000.0          # $5k minimum market liquidity

# Position management
LINEUP_CHECK_MINUTES_BEFORE_KICKOFF=65  # Stage 1: fetch lineups + recalculate EV
FORCE_CLOSE_MINUTES_BEFORE_KICKOFF=30   # Stage 2: unconditional force-close

# Leagues (API-Football IDs)
ACTIVE_LEAGUES=39,140,2     # EPL, La Liga, Champions League
```

## Project structure

```
src/sporting_edge/
├── agents/          # LangGraph nodes (data_collector, model_predictor, odds_analyzer,
│                    #   risk_manager, execution_agent, position_manager, bet_settler, report_agent)
├── api/             # FastAPI server + routers (pipeline, markets, positions)
├── backtesting/     # Historical replay engine
├── config/          # Settings (pydantic-settings) + structured logging
├── db/              # SQLAlchemy ORM models + async session factory
├── graph/           # LangGraph orchestrator + AgentState routing
├── models/          # Pydantic schemas (in-memory domain models)
└── tools/           # External API clients (football_api, polymarket_tools, polymarket_streamer)
migrations/          # Alembic SQL migrations
tests/               # pytest suite
```

## Safety model

Two independent gates must both be disabled before any real money moves:

| Setting | Default | Effect |
|---|---|---|
| `PAPER_TRADING=true` | true | Simulates all order execution |
| `EXECUTE_TRADES=false` | false | Never calls the CLOB even if paper_trading=false |

Additional risk controls:
- Global cooldown: 5s minimum between any two orders
- Per-event cooldown: 120s minimum between orders on the same condition
- Max 3 bets per league per day
- Daily loss limit (default $50)
- Force-close all positions 60 minutes before kickoff

## Backtesting

Before enabling live trading, validate the model on historical data:

```bash
sporting-edge backtest data/sample_backtest.csv
```

Output includes: ROI, hit rate, Sharpe ratio, Brier score (calibration), P&L by league.

## API endpoints

Once `sporting-edge serve` is running on port 8001:

| Method | Path | Description |
|---|---|---|
| POST | `/pipeline/trigger` | Trigger pipeline (async) |
| POST | `/pipeline/trigger/sync` | Trigger pipeline (wait for result) |
| GET | `/markets` | Active market conditions |
| GET | `/positions` | Open positions with current P&L |

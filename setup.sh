#!/usr/bin/env bash
# ============================================================
# Kalitron Sporting Edge — Quick Setup
# ============================================================
set -e

echo "==> Creating virtual environment"
python3 -m venv .venv
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --upgrade pip
pip install -e ".[dev,test]"

echo "==> Copying .env.example → .env (fill in your keys!)"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    ⚠️  Edit .env with your API keys before running"
fi

echo "==> Starting PostgreSQL via Docker"
docker-compose up -d db
echo "    Waiting for Postgres to be ready..."
sleep 5

echo "==> Applying migrations"
sporting-edge migrate

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your ANTHROPIC_API_KEY and API_FOOTBALL_KEY"
echo "  2. Run tests:       pytest"
echo "  3. Run pipeline:    sporting-edge run"
echo "  4. Start API:       sporting-edge serve"
echo "  5. Backtest:        sporting-edge backtest data/sample_backtest.csv"

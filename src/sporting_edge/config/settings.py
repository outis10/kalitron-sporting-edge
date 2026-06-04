"""
Centralised configuration using pydantic-settings.
All values are read from environment variables / .env file.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ─────────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    llm_model: str = "claude-sonnet-4-6"
    llm_temperature: float = 0.0

    # ── Football API ────────────────────────────────────────────────────────
    api_football_key: str = Field("", description="api-football.com key")
    api_football_base_url: str = "https://v3.football.api-sports.io"

    # ── Polymarket ──────────────────────────────────────────────────────────
    polymarket_private_key: str = ""
    polymarket_funder_address: str = ""
    # L2 API credentials (derived automatically if not provided)
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    # 0=EOA/MetaMask  1=Magic/email  2=Gnosis Safe proxy
    polymarket_signature_type: int = 0
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://sporting:edge@localhost:5432/sporting_edge"
    database_url_sync: str = "postgresql://sporting:edge@localhost:5432/sporting_edge"

    # ── Safety ───────────────────────────────────────────────────────────────
    paper_trading: bool = True
    execute_trades: bool = False

    # ── Risk ─────────────────────────────────────────────────────────────────
    bankroll_usd: float = 1000.0
    max_kelly_fraction: float = Field(0.25, description="Quarter Kelly")
    max_bet_pct_bankroll: float = 0.02
    min_ev_threshold: float = 0.05              # paper trading / backtesting
    min_ev_threshold_live: float = 0.08         # production — absorbs spread + slippage + model error
    min_ev_threshold_low_liquidity: float = 0.12  # markets $5k-$10k liquidity
    low_liquidity_threshold: float = 10_000.0   # below this → stricter EV required
    min_market_liquidity: float = 5000.0
    min_model_confidence: float = 0.60
    daily_loss_limit_usd: float = 50.0

    # ── Leagues ──────────────────────────────────────────────────────────────
    # Comma-separated API-Football league IDs
    active_leagues: str = "39,140,2"        # EPL=39, La Liga=140, UCL=2

    @property
    def active_league_ids(self) -> list[int]:
        return [int(x.strip()) for x in self.active_leagues.split(",") if x.strip()]

    # ── Position management ───────────────────────────────────────────────────
    take_profit_pct: float = Field(0.10, description="Close position at +10% price gain")
    stop_loss_pct: float = Field(0.05, description="Close position at -5% price drop")
    lineup_check_minutes_before_kickoff: int = Field(
        65, description="Stage 1: fetch lineups and recalculate EV; close if edge is gone"
    )
    force_close_minutes_before_kickoff: int = Field(
        30, description="Stage 2: unconditional force-close before kickoff"
    )

    # ── Scheduler ────────────────────────────────────────────────────────────
    data_refresh_minutes: int = 30
    signal_scan_minutes: int = 15
    position_check_minutes: int = 5   # how often position_manager runs

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["json", "pretty"] = "pretty"

    # ── LangSmith ────────────────────────────────────────────────────────────
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "sporting-edge"

    # ── App ──────────────────────────────────────────────────────────────────
    environment: Literal["development", "production"] = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8001

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def max_bet_usd(self) -> float:
        return self.bankroll_usd * self.max_bet_pct_bankroll

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @field_validator("execute_trades")
    @classmethod
    def warn_if_live(cls, v: bool) -> bool:
        if v:
            import warnings
            warnings.warn(
                "⚠️  EXECUTE_TRADES=true — real USDC will be spent on Polymarket!",
                stacklevel=2,
            )
        return v

    @property
    def clob_has_l2_creds(self) -> bool:
        """True when explicit L2 credentials are configured."""
        return bool(
            self.polymarket_api_key
            and self.polymarket_api_secret
            and self.polymarket_api_passphrase
        )

    def validate_for_live(self) -> None:
        """Call before enabling real execution; raises if misconfigured."""
        if not self.polymarket_private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required for live trading")
        if not self.polymarket_funder_address:
            raise ValueError("POLYMARKET_FUNDER_ADDRESS is required for live trading")
        if self.polymarket_signature_type not in (0, 1, 2):
            raise ValueError("POLYMARKET_SIGNATURE_TYPE must be 0 (EOA), 1 (Magic), or 2 (proxy)")
        if self.paper_trading:
            raise ValueError("Set PAPER_TRADING=false to enable live trading")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Convenience singleton — import this everywhere
settings = get_settings()

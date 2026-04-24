"""Configuration model — loads YAML defaults, overlays env, exposes typed objects.

The config is intentionally monolithic and frozen after load; any runtime
change (mode switch, lots-per-trade update) goes through `RuntimeState`
in engine.state rather than mutating this object.
"""
from __future__ import annotations

import os
from datetime import time as dtime
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_env_path() -> Path:
    """Return the .env path to load, in priority order:

      1. `$VOLSCALP_ENV_FILE` (absolute or CWD-relative) if set and exists.
      2. `~/Documents/shared/.env` — shared credentials across projects
         on this user's machine. Portable because `Path.home()` resolves
         to the current user's home directory on any OS.
      3. `./.env` — legacy repo-local fallback. Returned even if the file
         doesn't exist so downstream behaviour (empty creds) is unchanged
         for first-time users.
    """
    override = os.getenv("VOLSCALP_ENV_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p
    shared = Path.home() / "Documents" / "shared" / ".env"
    if shared.is_file():
        return shared
    return Path(".env")


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class IndexName(str, Enum):
    NIFTY = "NIFTY"
    BANKNIFTY = "BANKNIFTY"


class SessionConfig(BaseModel):
    timezone: str = "Asia/Kolkata"
    start: dtime = dtime(9, 30)
    end: dtime = dtime(15, 15)
    warmup_from: dtime = dtime(9, 15)

    @field_validator("start", "end", "warmup_from", mode="before")
    @classmethod
    def _parse_time(cls, v):
        if isinstance(v, str):
            h, m = v.split(":")
            return dtime(int(h), int(m))
        return v


class EngineConfig(BaseModel):
    cooldown_minutes: int = 0
    momentum_threshold_pct: float = 1.0
    strike_offset_ce: int = 6
    strike_offset_pe: int = -6
    base_leg_sl_pct: float = 15.0
    lazy_leg_sl_pct: float = 12.0
    lazy_enabled: bool = True
    entry_price_source: Literal["close", "next_open"] = "close"
    sl_price_source: Literal["close", "low"] = "low"
    lots_per_trade_paper: int = 1
    lots_per_trade_live: int = 1
    max_concurrent_cycles: int = 2


class MtmProfile(BaseModel):
    """Cycle exit thresholds.

    Aggregate-MTM (realised + unrealised) is checked every bar in this
    priority order:

        1. ``mtm <= -max_loss``   → MTM_MAX_LOSS
        2. ``mtm >= target``      → MTM_TARGET

    Lock-and-trail was evaluated and removed (2026-04): it did not
    improve P&L vs the straight max_loss/target pair in the 2y backtest
    and added complexity.
    """
    max_loss: float
    target: float


class InstrumentConfig(BaseModel):
    underlying_symbol: str
    strike_interval: int
    lot_size: int


class BrokerConfig(BaseModel):
    name: Literal["dhan"] = "dhan"
    feed_mode: Literal["quote", "ticker", "depth_20"] = "quote"
    ws_reconnect_backoff_s: list[float] = [1, 2, 4, 8, 16, 30]
    reconcile_interval_s: float = 1.0
    order_timeout_s: float = 5.0


class PaperConfig(BaseModel):
    fill_source: Literal["ltp", "best_bid_ask"] = "ltp"
    slippage_bps: float = 0.0


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    kill_switch_require_confirm: bool = True
    live_mode_require_confirm: bool = True


class PersistenceConfig(BaseModel):
    sqlite_path: str = "data/volscalp.db"
    bar_snapshots_enabled: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_output: bool = True
    path: str | None = "logs/volscalp.log"


class AppConfig(BaseModel):
    mode: Mode = Mode.PAPER
    indices: list[IndexName] = [IndexName.NIFTY, IndexName.BANKNIFTY]
    expiry: Literal["monthly", "weekly_current", "weekly_next"] = "monthly"
    session: SessionConfig = SessionConfig()
    engine: EngineConfig = EngineConfig()
    mtm_profiles: dict[IndexName, MtmProfile]
    instruments: dict[IndexName, InstrumentConfig]
    broker: BrokerConfig = BrokerConfig()
    paper: PaperConfig = PaperConfig()
    dashboard: DashboardConfig = DashboardConfig()
    persistence: PersistenceConfig = PersistenceConfig()
    logging: LoggingConfig = LoggingConfig()


class EnvSecrets(BaseSettings):
    """Secrets from .env — never logged, never persisted."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    DHAN_CLIENT_ID: str = ""
    DHAN_ACCESS_TOKEN: str = ""
    DHAN_PIN: str = ""
    DHAN_TOTP_SECRET: str = ""

    VOLSCALP_MODE: str = ""
    VOLSCALP_LOG_LEVEL: str = ""
    VOLSCALP_DATA_DIR: str = "./data"
    VOLSCALP_DASHBOARD_HOST: str = ""
    VOLSCALP_DASHBOARD_PORT: str = ""

    def has_dhan_credentials(self) -> bool:
        return bool(self.DHAN_CLIENT_ID and self.DHAN_ACCESS_TOKEN)


def load_config(path: Path | str = "configs/default.yaml") -> AppConfig:
    """Read YAML + env, return validated AppConfig."""
    env_path = resolve_env_path()
    load_dotenv(env_path, override=False)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = AppConfig.model_validate(raw)

    env = EnvSecrets(_env_file=str(env_path))
    if env.VOLSCALP_MODE:
        cfg = cfg.model_copy(update={"mode": Mode(env.VOLSCALP_MODE)})
    if env.VOLSCALP_LOG_LEVEL:
        cfg = cfg.model_copy(
            update={"logging": cfg.logging.model_copy(update={"level": env.VOLSCALP_LOG_LEVEL})}
        )
    if env.VOLSCALP_DASHBOARD_HOST or env.VOLSCALP_DASHBOARD_PORT:
        dashboard_update = {}
        if env.VOLSCALP_DASHBOARD_HOST:
            dashboard_update["host"] = env.VOLSCALP_DASHBOARD_HOST
        if env.VOLSCALP_DASHBOARD_PORT:
            dashboard_update["port"] = int(env.VOLSCALP_DASHBOARD_PORT)
        cfg = cfg.model_copy(update={"dashboard": cfg.dashboard.model_copy(update=dashboard_update)})

    return cfg


def load_secrets() -> EnvSecrets:
    env_path = resolve_env_path()
    load_dotenv(env_path, override=False)
    return EnvSecrets(_env_file=str(env_path))

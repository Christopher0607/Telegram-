"""Settings: secrets from .env, strategy from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


@dataclass
class ChannelCfg:
    name: str                              # t.me username, e.g. "IvanCryptotalk"
    enabled: bool = True
    margin_usdt: Optional[float] = None    # per-channel overrides of risk.*
    risk_usdt: Optional[float] = None
    max_leverage: Optional[int] = None
    require_stop_loss: Optional[bool] = None


@dataclass
class RiskCfg:
    sizing: str = "fixed_margin"           # fixed_margin | fixed_risk
    margin_usdt: float = 10.0              # fixed_margin: margin per trade
    risk_usdt: float = 5.0                 # fixed_risk: USDT lost if stop is hit
    max_notional_usdt: float = 500.0       # hard cap on position size
    default_leverage: int = 5
    max_leverage: int = 10
    max_loss_at_stop_pct: float = 60.0     # lower leverage so SL hits before this % of margin is gone
    require_stop_loss: bool = True
    default_stop_loss_pct: float = 3.0     # used only when require_stop_loss is false
    default_tp_rr: float = 2.0             # TP at N x stop distance when the signal has no TP (0 = none)
    max_open_positions: int = 3
    max_trades_per_day: int = 10
    daily_loss_limit_pct: float = 10.0
    max_entry_deviation_pct: float = 1.5
    signal_max_age_sec: int = 120
    dedup_hours: float = 12.0
    use_limit_orders: bool = True
    limit_order_ttl_min: int = 60
    tp_split: list[float] = field(default_factory=lambda: [0.5, 0.3, 0.2])
    move_sl_to_entry_after_tp1: bool = True
    follow_updates: bool = True
    symbol_whitelist: list[str] = field(default_factory=list)
    symbol_blacklist: list[str] = field(default_factory=list)


@dataclass
class LlmCfg:
    enabled: bool = False
    mode: str = "fallback"                 # fallback: only when regex finds nothing | always
    model: str = "claude-opus-5"


@dataclass
class Settings:
    dry_run: bool
    api_id: int
    api_hash: str
    phone: str
    session_path: str
    exchange_id: str
    exchange_key: str
    exchange_secret: str
    exchange_password: str
    testnet: bool
    margin_mode: str
    channels: list[ChannelCfg]
    risk: RiskCfg
    llm: LlmCfg
    notify_chat: str = "me"
    db_path: str = "data/tgtrader.db"
    auto_join: bool = True

    def channel(self, name: str) -> Optional[ChannelCfg]:
        for c in self.channels:
            if c.name.lower() == name.lower():
                return c
        return None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def load_settings(config_path: str = "config.yaml", env_path: str = ".env") -> Settings:
    load_dotenv(env_path)
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    ex = raw.get("exchange", {}) or {}
    channels = [ChannelCfg(**c) if isinstance(c, dict) else ChannelCfg(name=str(c))
                for c in raw.get("channels", [])]
    for c in channels:
        c.name = c.name.removeprefix("https://t.me/").removeprefix("@").strip("/")
    dry_env = _env("DRY_RUN")
    dry_run = raw.get("dry_run", True) if not dry_env else dry_env.lower() in ("1", "true", "yes")
    return Settings(
        dry_run=bool(dry_run),
        api_id=int(_env("TG_API_ID", "0") or 0),
        api_hash=_env("TG_API_HASH"),
        phone=_env("TG_PHONE"),
        session_path=_env("TG_SESSION", "data/tg"),
        exchange_id=_env("EXCHANGE_ID") or ex.get("id", "binance"),
        exchange_key=_env("EXCHANGE_API_KEY"),
        exchange_secret=_env("EXCHANGE_API_SECRET"),
        exchange_password=_env("EXCHANGE_API_PASSWORD"),
        testnet=bool(ex.get("testnet", False)),
        margin_mode=ex.get("margin_mode", "isolated"),
        channels=channels,
        risk=RiskCfg(**(raw.get("risk") or {})),
        llm=LlmCfg(**(raw.get("llm") or {})),
        notify_chat=raw.get("notify_chat", "me"),
        db_path=raw.get("db_path", "data/tgtrader.db"),
        auto_join=raw.get("auto_join", True),
    )

"""Optional Claude-based parser for signals the regex parser can't read.

Enabled with `llm.enabled: true` in config.yaml and ANTHROPIC_API_KEY in .env.
The model only extracts fields; every result still passes through the same
risk checks as regex-parsed signals.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from .parser import LONG, SHORT, Parsed, Signal, Update, sanitize

log = logging.getLogger(__name__)

SYSTEM = """You extract crypto futures trading instructions from Telegram channel posts (often Chinese).
Classify the post:
- "signal": a new trade call with a coin and a direction (做多/long or 做空/short).
- "update": an instruction about an already-open trade on a named coin: move stop (提損/移損 -> move_sl,
  保本 -> move_sl with price null), partial close (減倉 N% -> reduce), or full close (全部平倉 -> close).
- "none": ads, recruiting, results bragging, commentary, vague ideas ("今天想做空這個"), anything else.
Only report numbers that appear in the post. Never invent prices. Symbol is the base asset only (BTC, not BTCUSDT)."""

_NUM_OR_NULL = {"type": ["number", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["signal", "update", "none"]},
        "symbol": {"type": ["string", "null"]},
        "side": {"type": ["string", "null"], "enum": ["long", "short", None]},
        "market_order": {"type": "boolean"},
        "entry_low": _NUM_OR_NULL,
        "entry_high": _NUM_OR_NULL,
        "take_profits": {"type": "array", "items": {"type": "number"}},
        "stop_loss": _NUM_OR_NULL,
        "leverage": {"type": ["integer", "null"]},
        "update_action": {"type": ["string", "null"], "enum": ["move_sl", "reduce", "close", None]},
        "update_price": _NUM_OR_NULL,
        "update_percent": _NUM_OR_NULL,
    },
    "required": ["kind", "symbol", "side", "market_order", "entry_low", "entry_high", "take_profits",
                 "stop_loss", "leverage", "update_action", "update_price", "update_percent"],
    "additionalProperties": False,
}


class LlmParser:
    def __init__(self, model: str):
        self.model = model
        self.client = anthropic.AsyncAnthropic()

    async def parse(self, text: str) -> Parsed:
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": text}],
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIError as e:
            log.warning("LLM parse failed: %s", e)
            return None
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("LLM parse stopped: %s", resp.stop_reason)
            return None
        body = next((b.text for b in resp.content if b.type == "text"), None)
        if not body:
            return None
        return to_parsed(json.loads(body), text)


def to_parsed(d: dict, raw: str) -> Parsed:
    sym = (d.get("symbol") or "").upper().removesuffix("USDT").strip("$#/ ")
    if not sym:
        return None
    if d["kind"] == "signal" and d.get("side") in (LONG, SHORT):
        lo, hi = d.get("entry_low"), d.get("entry_high")
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        if lo is None and hi is not None:
            lo = hi
        sig = Signal(symbol=sym, side=d["side"], market=bool(d.get("market_order")),
                     entry_low=lo, entry_high=hi if hi is not None else lo,
                     take_profits=[float(x) for x in d.get("take_profits") or []],
                     stop_loss=d.get("stop_loss"), leverage=d.get("leverage"), raw=raw, source="llm")
        return sanitize(sig)
    if d["kind"] == "update" and d.get("update_action") in ("move_sl", "reduce", "close"):
        return Update(sym, d["update_action"], price=d.get("update_price"),
                      percent=d.get("update_percent"), raw=raw)
    return None

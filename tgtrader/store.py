"""SQLite persistence: seen signals, trades, daily equity."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT,
    symbol        TEXT,          -- ccxt market symbol, e.g. BTC/USDT:USDT
    side          TEXT,
    amount        REAL,          -- contracts
    notional      REAL,          -- USDT position value at entry
    entry_price   REAL,
    stop_loss     REAL,
    take_profits  TEXT,          -- json list
    leverage      INTEGER,
    status        TEXT,          -- pending | open | closed | cancelled | failed
    dry_run       INTEGER,
    entry_order   TEXT,
    sl_order      TEXT,
    tp_orders     TEXT,          -- json list
    tp_hits       INTEGER DEFAULT 0,
    pnl           REAL,          -- realised PnL in USDT (dry-run simulation, or estimate when live)
    raw           TEXT,
    note          TEXT,
    created_ts    REAL,
    updated_ts    REAL
);
CREATE TABLE IF NOT EXISTS equity (
    day   TEXT PRIMARY KEY,
    start REAL
);
"""


@dataclass
class Trade:
    id: Optional[int]
    channel: str
    symbol: str
    side: str
    amount: float
    entry_price: float
    stop_loss: Optional[float]
    take_profits: list[float]
    leverage: int
    status: str
    dry_run: bool
    entry_order: Optional[str] = None
    sl_order: Optional[str] = None
    tp_orders: list[str] = field(default_factory=list)
    notional: float = 0.0
    tp_hits: int = 0
    pnl: Optional[float] = None
    raw: str = ""
    note: str = ""
    created_ts: float = 0.0


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- dedup -------------------------------------------------------------
    def seen_recently(self, key: str, window_sec: float) -> bool:
        row = self.db.execute("SELECT ts FROM seen WHERE key=?", (key,)).fetchone()
        return bool(row and time.time() - row["ts"] < window_sec)

    def mark_seen(self, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO seen(key, ts) VALUES(?, ?)", (key, time.time()))
        self.db.commit()

    # --- trades ------------------------------------------------------------
    def add_trade(self, t: Trade) -> int:
        now = time.time()
        cur = self.db.execute(
            """INSERT INTO trades(channel, symbol, side, amount, notional, entry_price, stop_loss, take_profits,
               leverage, status, dry_run, entry_order, sl_order, tp_orders, tp_hits, pnl, raw, note,
               created_ts, updated_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.channel, t.symbol, t.side, t.amount, t.notional, t.entry_price, t.stop_loss,
             json.dumps(t.take_profits), t.leverage, t.status, int(t.dry_run), t.entry_order, t.sl_order,
             json.dumps(t.tp_orders), t.tp_hits, t.pnl, t.raw, t.note, now, now))
        self.db.commit()
        t.id = cur.lastrowid
        return t.id

    def update_trade(self, trade_id: int, **fields) -> None:
        if not fields:
            return
        for k in ("take_profits", "tp_orders"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        fields["updated_ts"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE trades SET {cols} WHERE id=?", (*fields.values(), trade_id))
        self.db.commit()

    def _row(self, r: sqlite3.Row) -> Trade:
        return Trade(
            id=r["id"], channel=r["channel"], symbol=r["symbol"], side=r["side"], amount=r["amount"],
            entry_price=r["entry_price"], stop_loss=r["stop_loss"],
            take_profits=json.loads(r["take_profits"] or "[]"), leverage=r["leverage"], status=r["status"],
            dry_run=bool(r["dry_run"]), entry_order=r["entry_order"], sl_order=r["sl_order"],
            tp_orders=json.loads(r["tp_orders"] or "[]"), notional=r["notional"] or 0.0,
            tp_hits=r["tp_hits"] or 0, pnl=r["pnl"], raw=r["raw"] or "", note=r["note"] or "",
            created_ts=r["created_ts"])

    def active_trades(self) -> list[Trade]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE status IN ('pending','open') ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def active_trade_for(self, base_or_symbol: str) -> Optional[Trade]:
        key = base_or_symbol.upper()
        for t in self.active_trades():
            if t.symbol.upper() == key or t.symbol.split("/")[0].upper() == key:
                return t
        return None

    def trades_since(self, ts: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM trades WHERE created_ts>=? AND status NOT IN ('failed')", (ts,)
        ).fetchone()[0]

    def recent_trades(self, limit: int = 10) -> list[Trade]:
        rows = self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def channel_stats(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT channel,
                      COUNT(*)                                        AS trades,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)        AS wins,
                      SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)       AS losses,
                      ROUND(COALESCE(SUM(pnl), 0), 2)                 AS pnl
               FROM trades WHERE status='closed' GROUP BY channel ORDER BY pnl DESC""").fetchall()

    # --- equity ------------------------------------------------------------
    def day_start_equity(self, day: str, current: float) -> float:
        row = self.db.execute("SELECT start FROM equity WHERE day=?", (day,)).fetchone()
        if row:
            return row["start"]
        self.db.execute("INSERT INTO equity(day, start) VALUES(?, ?)", (day, current))
        self.db.commit()
        return current

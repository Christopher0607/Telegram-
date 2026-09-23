"""Turn a parsed signal + live price into a concrete, risk-checked order plan."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .config import ChannelCfg, RiskCfg
from .parser import LONG, Signal


class Rejected(Exception):
    """Signal will not be traded; message explains why."""


@dataclass
class Plan:
    side: str                    # long | short
    order_type: str              # market | limit
    entry_price: float           # expected fill (market) or limit price
    notional: float              # USDT
    leverage: int
    stop_loss: float
    take_profits: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def margin(self) -> float:
        return self.notional / self.leverage

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss) / self.entry_price

    @property
    def loss_at_stop(self) -> float:
        return self.notional * self.stop_distance


def build_plan(sig: Signal, price: float, risk: RiskCfg, ch: Optional[ChannelCfg] = None) -> Plan:
    ch = ch or ChannelCfg(name="")
    sym = sig.symbol.upper()
    if risk.symbol_whitelist and sym not in {s.upper() for s in risk.symbol_whitelist}:
        raise Rejected(f"{sym} 不在白名单")
    if sym in {s.upper() for s in risk.symbol_blacklist}:
        raise Rejected(f"{sym} 在黑名单")
    if price <= 0:
        raise Rejected("无法获取价格")

    is_long = sig.side == LONG
    tol = risk.max_entry_deviation_pct / 100
    notes: list[str] = []

    # Sanity: a signal whose prices are nowhere near the market is misparsed or a different contract.
    ref = sig.entry_ref or sig.stop_loss
    if ref and abs(price - ref) / ref > 0.3:
        raise Rejected(f"信号价格 {ref} 与市价 {price} 相差过大")

    # --- entry --------------------------------------------------------------
    order_type, entry = "market", price
    if sig.entry_low is not None:
        lo, hi = sig.entry_low, sig.entry_high or sig.entry_low
        if is_long:
            chased = price > hi * (1 + tol)
            limit_px = hi
        else:
            chased = price < lo * (1 - tol)
            limit_px = lo
        if chased:
            if not risk.use_limit_orders:
                raise Rejected(f"价格已偏离进场区间 {lo}-{hi}（市价 {price}）")
            order_type, entry = "limit", limit_px
            notes.append(f"市价已偏离，挂限价 {limit_px}")

    # --- stop loss ------------------------------------------------------------
    require_sl = risk.require_stop_loss if ch.require_stop_loss is None else ch.require_stop_loss
    sl = sig.stop_loss
    if sl is None:
        if require_sl:
            raise Rejected("信号没有止损，已跳过（require_stop_loss=true）")
        pct = risk.default_stop_loss_pct / 100
        sl = entry * (1 - pct) if is_long else entry * (1 + pct)
        notes.append(f"信号无止损，使用默认 {risk.default_stop_loss_pct}%")
    if (is_long and sl >= entry) or (not is_long and sl <= entry):
        raise Rejected(f"止损 {sl} 已在当前价 {entry} 的错误一侧")
    dist = abs(entry - sl) / entry
    if dist > 0.25:
        raise Rejected(f"止损距离 {dist:.1%} 过大")

    # --- leverage ------------------------------------------------------------
    max_lev = ch.max_leverage or risk.max_leverage
    lev = min(sig.leverage or risk.default_leverage, max_lev)
    lev_by_stop = math.floor(risk.max_loss_at_stop_pct / 100 / dist + 1e-9) if dist > 0 else lev
    if lev_by_stop < lev:
        notes.append(f"杠杆由 {lev}x 降至 {max(lev_by_stop, 1)}x（止损前不被强平）")
        lev = lev_by_stop
    lev = max(int(lev), 1)
    if sig.leverage and sig.leverage > lev:
        notes.append(f"信号杠杆 {sig.leverage}x，实际使用 {lev}x")

    # --- size ------------------------------------------------------------------
    if risk.sizing == "fixed_risk":
        risk_usdt = ch.risk_usdt or risk.risk_usdt
        notional = risk_usdt / dist
    else:
        margin = ch.margin_usdt or risk.margin_usdt
        notional = margin * lev
    if notional > risk.max_notional_usdt:
        notes.append(f"仓位价值封顶 {risk.max_notional_usdt}U")
        notional = risk.max_notional_usdt

    # --- take profits ----------------------------------------------------------
    tps = [tp for tp in sig.take_profits if (tp > entry if is_long else tp < entry)]
    if not tps and risk.default_tp_rr > 0:
        move = entry * dist * risk.default_tp_rr
        tps = [entry + move if is_long else entry - move]
        notes.append(f"信号无止盈，使用 {risk.default_tp_rr}R")

    return Plan(side=sig.side, order_type=order_type, entry_price=entry, notional=notional,
                leverage=lev, stop_loss=sl, take_profits=tps, notes=notes)


def split_amounts(total: float, n_tps: int, weights: list[float]) -> list[float]:
    """Split a position across n take-profit levels using weights (renormalised)."""
    if n_tps <= 0:
        return []
    w = (weights or [1.0])[:n_tps]
    while len(w) < n_tps:
        w.append(w[-1])
    s = sum(w)
    parts = [total * x / s for x in w]
    parts[-1] = total - sum(parts[:-1])
    return parts

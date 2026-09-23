"""Exchange execution via ccxt (USDT-margined perpetuals), plus dry-run simulation."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import ccxt.async_support as ccxt

from .config import Settings
from .parser import LONG, Signal, Update
from .risk import Plan, Rejected, build_plan, split_amounts
from .store import Store, Trade

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]

# Some exchanges list low-priced coins as 1000PEPE etc. -> signal prices must be scaled.
_PREFIXES = [("", 1.0), ("1000", 1000.0), ("10000", 10000.0), ("1000000", 1_000_000.0), ("1M", 1_000_000.0)]


def _fmt(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.8g}"


class Trader:
    def __init__(self, settings: Settings, store: Store, notify: Notify):
        self.s = settings
        self.risk = settings.risk
        self.store = store
        self.notify = notify
        self.ex: Optional[ccxt.Exchange] = None
        self.lock = asyncio.Lock()
        self._watchers: set[asyncio.Task] = set()

    @property
    def dry(self) -> bool:
        return self.s.dry_run

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        ex_id = "binanceusdm" if self.s.exchange_id == "binance" else self.s.exchange_id
        klass = getattr(ccxt, ex_id)
        cfg = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        if self.s.exchange_key:
            cfg.update(apiKey=self.s.exchange_key, secret=self.s.exchange_secret)
            if self.s.exchange_password:
                cfg["password"] = self.s.exchange_password
        elif not self.dry:
            raise RuntimeError("实盘模式需要 EXCHANGE_API_KEY / EXCHANGE_API_SECRET")
        self.ex = klass(cfg)
        if self.s.testnet:
            self.ex.set_sandbox_mode(True)
        await self.ex.load_markets()
        log.info("exchange %s ready, %d markets, dry_run=%s", ex_id, len(self.ex.markets), self.dry)
        if not self.dry:
            await self.ex.fetch_balance()  # fail fast on bad keys
            for t in self.store.active_trades():
                if t.status == "pending" and t.entry_order:
                    self._spawn(self._watch_limit(t.id))

    async def close(self) -> None:
        for task in self._watchers:
            task.cancel()
        if self.ex:
            await self.ex.close()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    # ---------------------------------------------------------------- helpers
    def resolve(self, base: str) -> tuple[str, float]:
        base = base.upper()
        for prefix, scale in _PREFIXES:
            sym = f"{prefix}{base}/USDT:USDT"
            m = self.ex.markets.get(sym)
            if m and m.get("active", True) is not False and m.get("swap"):
                return sym, scale
        raise Rejected(f"交易所没有 {base} 的 USDT 永续合约")

    async def price(self, symbol: str) -> float:
        t = await self.ex.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close") or 0)

    def _contracts(self, symbol: str, notional: float, price: float) -> float:
        m = self.ex.market(symbol)
        size = float(m.get("contractSize") or 1)
        amount = float(self.ex.amount_to_precision(symbol, notional / (price * size)))
        min_amt = (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        min_cost = (m.get("limits", {}).get("cost", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amt:
            raise Rejected(f"仓位太小（{amount} < 最小 {min_amt}），请提高 margin_usdt")
        if min_cost and amount * price * size < min_cost:
            raise Rejected(f"名义价值低于交易所最小值 {min_cost}U")
        return amount

    def _min_amount(self, symbol: str) -> float:
        m = self.ex.market(symbol)
        return (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0

    def _px(self, symbol: str, p: float) -> float:
        return float(self.ex.price_to_precision(symbol, p))

    @staticmethod
    def _close_side(side: str) -> str:
        return "sell" if side == LONG else "buy"

    async def _position_size(self, symbol: str) -> float:
        positions = await self.ex.fetch_positions([symbol])
        return sum(abs(float(p.get("contracts") or 0)) for p in positions if p.get("symbol") == symbol)

    async def _open_position_count(self) -> int:
        if self.dry:
            return len(self.store.active_trades())
        positions = await self.ex.fetch_positions()
        live = {p["symbol"] for p in positions if abs(float(p.get("contracts") or 0)) > 0}
        pending = {t.symbol for t in self.store.active_trades() if t.status == "pending"}
        return len(live | pending)

    async def equity(self) -> Optional[float]:
        if self.dry or not self.s.exchange_key:
            return None
        bal = await self.ex.fetch_balance()
        return float((bal.get("USDT") or {}).get("total") or 0)

    async def _check_daily_limits(self) -> None:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.store.trades_since(day_start.timestamp()) >= self.risk.max_trades_per_day:
            raise Rejected(f"今日已达最大开仓次数 {self.risk.max_trades_per_day}")
        eq = await self.equity()
        if eq:
            start = self.store.day_start_equity(day_start.date().isoformat(), eq)
            if start > 0 and (start - eq) / start * 100 >= self.risk.daily_loss_limit_pct:
                raise Rejected(f"今日亏损已达 {self.risk.daily_loss_limit_pct}% 上限，停止开新仓")

    # ------------------------------------------------------------------ open
    async def open_signal(self, sig: Signal, channel: str) -> str:
        async with self.lock:
            symbol, scale = self.resolve(sig.symbol)
            if scale != 1:
                sig = dataclasses.replace(
                    sig,
                    entry_low=sig.entry_low * scale if sig.entry_low else None,
                    entry_high=sig.entry_high * scale if sig.entry_high else None,
                    stop_loss=sig.stop_loss * scale if sig.stop_loss else None,
                    take_profits=[tp * scale for tp in sig.take_profits])
            if self.store.active_trade_for(symbol) or (not self.dry and await self._position_size(symbol) > 0):
                raise Rejected(f"{symbol} 已有持仓/挂单，跳过")
            if await self._open_position_count() >= self.risk.max_open_positions:
                raise Rejected(f"持仓数已达上限 {self.risk.max_open_positions}")
            await self._check_daily_limits()

            price = await self.price(symbol)
            plan = build_plan(sig, price, self.risk, self.s.channel(channel))
            plan.entry_price = self._px(symbol, plan.entry_price)
            plan.stop_loss = self._px(symbol, plan.stop_loss)
            plan.take_profits = [self._px(symbol, tp) for tp in plan.take_profits]
            amount = self._contracts(symbol, plan.notional, plan.entry_price)

            trade = Trade(id=None, channel=channel, symbol=symbol, side=plan.side, amount=amount,
                          notional=plan.notional, entry_price=plan.entry_price, stop_loss=plan.stop_loss,
                          take_profits=plan.take_profits, leverage=plan.leverage,
                          status="pending" if plan.order_type == "limit" else "open",
                          dry_run=self.dry, raw=sig.raw, note="; ".join(plan.notes))
            if self.dry:
                self.store.add_trade(trade)
                return self._describe("🧪 [模拟] 开仓", trade, plan)
            return await self._open_live(trade, plan)

    async def _open_live(self, trade: Trade, plan: Plan) -> str:
        sym = trade.symbol
        try:
            await self.ex.set_margin_mode(self.s.margin_mode, sym, {"leverage": plan.leverage})
        except Exception as e:  # already set / not supported
            log.debug("set_margin_mode: %s", e)
        try:
            await self.ex.set_leverage(plan.leverage, sym)
        except Exception as e:
            log.warning("set_leverage failed: %s", e)

        side = "buy" if trade.side == LONG else "sell"
        price = plan.entry_price if plan.order_type == "limit" else None
        order = await self.ex.create_order(sym, plan.order_type, side, trade.amount, price)
        trade.entry_order = str(order["id"])
        if plan.order_type == "market":
            filled = float(order.get("filled") or trade.amount)
            if order.get("average"):
                trade.entry_price = float(order["average"])
            trade.amount = filled
            self.store.add_trade(trade)
            await self._protect(trade, filled)
            return self._describe("✅ 已开仓", trade, plan)
        self.store.add_trade(trade)
        self._spawn(self._watch_limit(trade.id))
        return self._describe("⏳ 已挂限价单", trade, plan)

    async def _protect(self, trade: Trade, amount: float) -> None:
        """Place reduce-only stop-loss and take-profit orders. Closes the position if the SL fails."""
        sym, close = trade.symbol, self._close_side(trade.side)
        try:
            sl = await self.ex.create_order(sym, "market", close, amount, None,
                                            {"stopLossPrice": trade.stop_loss, "reduceOnly": True})
        except Exception as e:
            log.exception("stop-loss placement failed")
            await self.ex.create_order(sym, "market", close, amount, None, {"reduceOnly": True})
            self.store.update_trade(trade.id, status="failed", note=f"止损下单失败，已市价平仓: {e}")
            await self.notify(f"🚨 {sym} 止损下单失败，已立即市价平仓。错误: {e}")
            return

        tp_ids: list[str] = []
        parts = split_amounts(amount, len(trade.take_profits), self.risk.tp_split)
        min_amt = self._min_amount(sym)
        merged = list(zip(trade.take_profits, parts))

        def too_small(q: float) -> bool:
            return float(self.ex.amount_to_precision(sym, q)) < max(min_amt, 1e-12)

        # Chunks below the exchange minimum: fold the furthest level into the one before it.
        while len(merged) > 1 and any(too_small(p) for _, p in merged):
            _, extra = merged.pop()
            merged[-1] = (merged[-1][0], merged[-1][1] + extra)
        for tp, part in merged:
            qty = float(self.ex.amount_to_precision(sym, part))
            if qty <= 0:
                continue
            try:
                o = await self.ex.create_order(sym, "market", close, qty, None,
                                               {"takeProfitPrice": tp, "reduceOnly": True})
                tp_ids.append(str(o["id"]))
            except Exception as e:
                log.warning("TP %s failed: %s", tp, e)
                await self.notify(f"⚠️ {sym} 止盈单 {tp} 下单失败: {e}")
        self.store.update_trade(trade.id, status="open", amount=amount, entry_price=trade.entry_price,
                                sl_order=str(sl["id"]), tp_orders=tp_ids)

    async def _watch_limit(self, trade_id: int) -> None:
        trade = next((t for t in self.store.active_trades() if t.id == trade_id), None)
        if not trade:
            return
        deadline = trade.created_ts + self.risk.limit_order_ttl_min * 60
        sym = trade.symbol
        try:
            order = None
            while time.time() < deadline:
                await asyncio.sleep(15)
                order = await self.ex.fetch_order(trade.entry_order, sym)
                if order["status"] in ("closed", "canceled", "cancelled", "rejected", "expired"):
                    break
            if not order or order["status"] == "open":
                try:
                    await self.ex.cancel_order(trade.entry_order, sym)
                except Exception as e:
                    log.debug("cancel limit: %s", e)
                order = await self.ex.fetch_order(trade.entry_order, sym)
            filled = float(order.get("filled") or 0)
            async with self.lock:
                if filled > 0:
                    trade.entry_price = float(order.get("average") or trade.entry_price)
                    await self._protect(trade, filled)
                    await self.notify(f"✅ {sym} 限价单成交 {filled} @ {_fmt(trade.entry_price)}，已挂止损/止盈")
                else:
                    self.store.update_trade(trade.id, status="cancelled", note="限价单超时未成交")
                    await self.notify(f"⌛ {sym} 限价单 {self.risk.limit_order_ttl_min} 分钟未成交，已撤单")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("limit watcher failed")
            await self.notify(f"🚨 {sym} 限价单监控出错: {e}（请手动检查）")

    # ---------------------------------------------------------------- updates
    async def apply_update(self, u: Update, channel: str) -> str:
        async with self.lock:
            symbol, scale = self.resolve(u.symbol)
            trade = self.store.active_trade_for(symbol)
            if not trade or trade.status != "open":
                raise Rejected(f"{symbol} 没有由本机器人开的持仓，忽略指令")
            if trade.channel.lower() != channel.lower():
                raise Rejected(f"{symbol} 持仓来自 {trade.channel}，忽略 {channel} 的指令")
            if u.action == "move_sl":
                new_sl = u.price * scale if u.price else trade.entry_price
                return await self._move_sl(trade, self._px(symbol, new_sl))
            if u.action == "reduce":
                return await self._reduce(trade, (u.percent or 0) / 100)
            if u.action == "close":
                return await self._close(trade, "频道指令全部平仓")
        raise Rejected(f"未知指令 {u.action}")

    async def _move_sl(self, trade: Trade, new_sl: float) -> str:
        price = await self.price(trade.symbol)
        if (trade.side == LONG and new_sl >= price) or (trade.side != LONG and new_sl <= price):
            raise Rejected(f"新止损 {new_sl} 已越过当前价 {price}")
        if not self.dry:
            await self._cancel(trade.sl_order, trade.symbol, trigger=True)
            size = await self._position_size(trade.symbol)
            if size <= 0:
                raise Rejected("仓位已不存在")
            o = await self.ex.create_order(trade.symbol, "market", self._close_side(trade.side), size, None,
                                           {"stopLossPrice": new_sl, "reduceOnly": True})
            self.store.update_trade(trade.id, sl_order=str(o["id"]))
        self.store.update_trade(trade.id, stop_loss=new_sl)
        return f"🔧 {'[模拟] ' if self.dry else ''}{trade.symbol} 止损移至 {_fmt(new_sl)}"

    async def _reduce(self, trade: Trade, frac: float) -> str:
        if not 0 < frac < 1:
            return await self._close(trade, "频道指令平仓")
        price = await self.price(trade.symbol)
        if self.dry:
            part = trade.notional * frac
            pnl = self._pnl(trade, part, price)
            self.store.update_trade(trade.id, notional=trade.notional - part, pnl=(trade.pnl or 0) + pnl)
            return f"✂️ [模拟] {trade.symbol} 减仓 {frac:.0%} @ {_fmt(price)}，盈亏 {pnl:+.2f}U"
        size = await self._position_size(trade.symbol)
        qty = float(self.ex.amount_to_precision(trade.symbol, size * frac))
        if qty <= 0:
            raise Rejected("减仓数量过小")
        await self.ex.create_order(trade.symbol, "market", self._close_side(trade.side), qty, None,
                                   {"reduceOnly": True})
        return f"✂️ {trade.symbol} 已减仓 {frac:.0%}（{qty}）@ ~{_fmt(price)}"

    async def _close(self, trade: Trade, reason: str) -> str:
        price = await self.price(trade.symbol)
        if self.dry:
            pnl = (trade.pnl or 0) + (self._pnl(trade, trade.notional, price) if trade.status == "open" else 0)
            self.store.update_trade(trade.id, status="closed" if trade.status == "open" else "cancelled",
                                    pnl=pnl, note=reason)
            return f"🧪 [模拟] {trade.symbol} 平仓 @ {_fmt(price)}（{reason}），累计盈亏 {pnl:+.2f}U"
        await self._cancel_all(trade.symbol)
        size = await self._position_size(trade.symbol)
        if size > 0:
            await self.ex.create_order(trade.symbol, "market", self._close_side(trade.side), size, None,
                                       {"reduceOnly": True})
        self.store.update_trade(trade.id, status="closed", note=reason)
        return f"🛑 {trade.symbol} 已平仓 @ ~{_fmt(price)}（{reason}）"

    async def close_all(self) -> list[str]:
        out = []
        async with self.lock:
            for t in self.store.active_trades():
                try:
                    out.append(await self._close(t, "手动 /closeall"))
                except Exception as e:
                    out.append(f"❌ {t.symbol} 平仓失败: {e}")
        return out

    async def _cancel(self, order_id: Optional[str], symbol: str, trigger: bool = False) -> None:
        if not order_id:
            return
        for params in (({"trigger": True}, {}) if trigger else ({}, {"trigger": True})):
            try:
                await self.ex.cancel_order(order_id, symbol, params)
                return
            except Exception as e:
                log.debug("cancel %s %s: %s", order_id, params, e)

    async def _cancel_all(self, symbol: str) -> None:
        for params in ({}, {"trigger": True}):
            try:
                await self.ex.cancel_all_orders(symbol, params)
            except Exception as e:
                log.debug("cancel_all %s %s: %s", symbol, params, e)

    # ------------------------------------------------------------------- sync
    @staticmethod
    def _pnl(trade: Trade, notional_part: float, exit_price: float) -> float:
        move = (exit_price - trade.entry_price) / trade.entry_price
        return notional_part * (move if trade.side == LONG else -move)

    async def sync(self) -> None:
        """Periodic housekeeping: detect closed positions, move SL to break-even, simulate dry-run fills."""
        for t in self.store.active_trades():
            try:
                if t.dry_run:
                    await self._sync_dry(t)
                elif t.status == "open":
                    await self._sync_live(t)
            except Exception as e:
                log.warning("sync %s: %s", t.symbol, e)

    async def _sync_live(self, t: Trade) -> None:
        async with self.lock:
            size = await self._position_size(t.symbol)
            if size <= 0:
                await self._cancel_all(t.symbol)
                self.store.update_trade(t.id, status="closed")
                await self.notify(f"🏁 {t.symbol} 仓位已结束（止盈/止损触发），已撤销剩余挂单")
                return
            if (t.tp_hits == 0 and size < t.amount * 0.98 and self.risk.move_sl_to_entry_after_tp1
                    and len(t.take_profits) > 1):
                self.store.update_trade(t.id, tp_hits=1)
                try:
                    msg = await self._move_sl(t, self._px(t.symbol, t.entry_price))
                    await self.notify(f"🎯 {t.symbol} 第一止盈已成交。{msg}（保本）")
                except Rejected as e:
                    await self.notify(f"🎯 {t.symbol} 第一止盈已成交，保本止损未设置: {e}")

    async def _sync_dry(self, t: Trade) -> None:
        price = await self.price(t.symbol)
        is_long = t.side == LONG
        if t.status == "pending":
            if (is_long and price <= t.entry_price) or (not is_long and price >= t.entry_price):
                self.store.update_trade(t.id, status="open")
                await self.notify(f"🧪 [模拟] {t.symbol} 限价 {_fmt(t.entry_price)} 成交")
            elif time.time() > t.created_ts + self.risk.limit_order_ttl_min * 60:
                self.store.update_trade(t.id, status="cancelled", note="限价单超时")
            return

        parts = split_amounts(t.notional, len(t.take_profits), self.risk.tp_split) or [t.notional]
        pnl, hits, sl = t.pnl or 0.0, t.tp_hits, t.stop_loss
        while hits < len(t.take_profits):
            tp = t.take_profits[hits]
            if (is_long and price >= tp) or (not is_long and price <= tp):
                pnl += self._pnl(t, parts[hits], tp)
                hits += 1
                if hits == 1 and self.risk.move_sl_to_entry_after_tp1:
                    sl = t.entry_price
                await self.notify(f"🧪 [模拟] {t.symbol} 止盈{hits} {_fmt(tp)} 触发")
            else:
                break
        closed = hits >= len(t.take_profits) and t.take_profits
        if not closed and sl is not None and ((is_long and price <= sl) or (not is_long and price >= sl)):
            remaining = sum(parts[hits:]) if t.take_profits else t.notional
            pnl += self._pnl(t, remaining, sl)
            closed = True
            await self.notify(f"🧪 [模拟] {t.symbol} 止损 {_fmt(sl)} 触发，本单盈亏 {pnl:+.2f}U")
        elif closed:
            await self.notify(f"🧪 [模拟] {t.symbol} 全部止盈，本单盈亏 {pnl:+.2f}U")
        if closed or hits != t.tp_hits:
            self.store.update_trade(t.id, tp_hits=hits, pnl=pnl, stop_loss=sl,
                                    status="closed" if closed else "open")

    # ----------------------------------------------------------------- report
    def _describe(self, title: str, t: Trade, plan: Plan) -> str:
        lines = [
            f"{title} [{t.channel}]",
            f"{t.symbol} {'做多' if t.side == LONG else '做空'} {t.leverage}x",
            f"{'限价' if plan.order_type == 'limit' else '市价'} {_fmt(t.entry_price)}  数量 {t.amount}",
            f"仓位 {plan.notional:.1f}U  保证金 {plan.margin:.1f}U  止损亏损约 {plan.loss_at_stop:.2f}U",
            f"止损 {_fmt(t.stop_loss)}  止盈 {' / '.join(_fmt(x) for x in t.take_profits) or '-'}",
        ]
        if plan.notes:
            lines.append("备注: " + "; ".join(plan.notes))
        return "\n".join(lines)

    async def status(self) -> str:
        lines = [f"模式: {'🧪 模拟 (DRY_RUN)' if self.dry else '💰 实盘'}  交易所: {self.s.exchange_id}"]
        eq = await self.equity()
        if eq is not None:
            lines.append(f"账户权益: {eq:.2f} USDT")
        active = self.store.active_trades()
        lines.append(f"持仓/挂单: {len(active)}")
        for t in active:
            lines.append(f" • #{t.id} {t.symbol} {t.side} {t.status} 入场 {_fmt(t.entry_price)} "
                         f"止损 {_fmt(t.stop_loss)} [{t.channel}]")
        stats = self.store.channel_stats()
        if stats:
            lines.append("各频道战绩（已平仓）:")
            for r in stats:
                lines.append(f" • {r['channel']}: {r['trades']} 单, 胜 {r['wins']} 负 {r['losses']}, "
                             f"盈亏 {r['pnl']:+.2f}U")
        return "\n".join(lines)

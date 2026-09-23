"""Entry point: listen to Telegram channels and trade their signals.

    python -m tgtrader.main            # run the bot
    python -m tgtrader.main --login    # first-time Telegram login (interactive)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import signal as os_signal
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.tl.functions.channels import JoinChannelRequest

from .config import Settings, load_settings
from .llm_parser import LlmParser
from .parser import Signal, Update, parse_message
from .risk import Rejected
from .store import Store
from .trader import Trader

log = logging.getLogger("tgtrader")

HELP = """指令（发到你自己的「收藏夹 / Saved Messages」）:
/status    查看模式、持仓、各频道战绩
/pause     暂停跟单（不再开新仓）
/resume    恢复跟单
/closeall  平掉本机器人开的所有仓位
/recent    最近 10 笔记录
/help      帮助"""


class Bot:
    def __init__(self, s: Settings):
        self.s = s
        Path(s.session_path).parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(s.session_path, s.api_id, s.api_hash)
        self.store = Store(s.db_path)
        self.trader = Trader(s, self.store, self.notify)
        self.llm = LlmParser(s.llm.model) if s.llm.enabled else None
        self.paused = False
        self.channel_ids: dict[int, str] = {}

    async def notify(self, text: str) -> None:
        log.info("notify: %s", text.replace("\n", " | "))
        try:
            await self.client.send_message(self.s.notify_chat, text)
        except Exception as e:
            log.warning("notify failed: %s", e)

    # ------------------------------------------------------------- channels
    async def resolve_channels(self) -> None:
        for ch in self.s.channels:
            if not ch.enabled:
                continue
            try:
                ent = await self.client.get_entity(ch.name)
            except Exception as e:
                log.error("cannot resolve channel %s: %s", ch.name, e)
                await self.notify(f"⚠️ 找不到频道 {ch.name}: {e}")
                continue
            if self.s.auto_join and getattr(ent, "left", False):
                try:
                    await self.client(JoinChannelRequest(ent))
                    log.info("joined %s", ch.name)
                except Exception as e:
                    log.warning("join %s failed: %s", ch.name, e)
            peer_id = utils.get_peer_id(ent)
            self.channel_ids[peer_id] = ch.name
            log.info("watching %s (id=%s)", ch.name, peer_id)

    # ------------------------------------------------------------- messages
    async def on_channel_message(self, event) -> None:
        channel = self.channel_ids.get(event.chat_id)
        if channel is None:
            return
        text = event.raw_text or ""
        age = (datetime.now(timezone.utc) - event.message.date).total_seconds()
        log.info("[%s] (%.0fs) %s", channel, age, text[:200].replace("\n", " | "))
        if age > self.s.risk.signal_max_age_sec:
            log.info("skip: message too old")
            return

        parsed = None if (self.llm and self.s.llm.mode == "always") else parse_message(text)
        if parsed is None and self.llm and _worth_llm(text):
            parsed = await self.llm.parse(text)
        if parsed is None:
            return

        if isinstance(parsed, Signal):
            await self.handle_signal(parsed, channel)
        elif isinstance(parsed, Update) and self.s.risk.follow_updates:
            await self.handle_update(parsed, channel)

    async def handle_signal(self, sig: Signal, channel: str) -> None:
        key = hashlib.sha1(f"{sig.symbol}|{sig.side}|{sig.entry_low}|{sig.stop_loss}".encode()).hexdigest()
        if self.store.seen_recently(key, self.s.risk.dedup_hours * 3600):
            log.info("skip duplicate signal %s %s", sig.symbol, sig.side)
            return
        self.store.mark_seen(key)
        summary = (f"📡 [{channel}] {sig.symbol} {'做多' if sig.side == 'long' else '做空'}"
                   f" 进场 {sig.entry_low}-{sig.entry_high} 止损 {sig.stop_loss} 止盈 {sig.take_profits}"
                   f" 杠杆 {sig.leverage} ({sig.source})")
        if self.paused:
            await self.notify(summary + "\n⏸ 已暂停，未下单")
            return
        try:
            msg = await self.trader.open_signal(sig, channel)
            await self.notify(msg)
        except Rejected as e:
            await self.notify(f"{summary}\n⛔ 未下单: {e}")
        except Exception as e:
            log.exception("open failed")
            await self.notify(f"{summary}\n❌ 下单出错: {e}")

    async def handle_update(self, u: Update, channel: str) -> None:
        try:
            msg = await self.trader.apply_update(u, channel)
            await self.notify(f"[{channel}] {msg}")
        except Rejected as e:
            log.info("update ignored: %s", e)
        except Exception as e:
            log.exception("update failed")
            await self.notify(f"❌ [{channel}] 执行 {u.symbol} {u.action} 出错: {e}")

    async def on_command(self, event) -> None:
        cmd = (event.raw_text or "").strip().split()[0].lower()
        if cmd == "/status":
            await event.reply(await self.trader.status() + ("\n⏸ 已暂停" if self.paused else ""))
        elif cmd == "/pause":
            self.paused = True
            await event.reply("⏸ 已暂停跟单（已有仓位的止损止盈仍然有效）")
        elif cmd == "/resume":
            self.paused = False
            await event.reply("▶️ 已恢复跟单")
        elif cmd == "/closeall":
            self.paused = True
            res = await self.trader.close_all()
            await event.reply(("\n".join(res) or "没有持仓") + "\n⏸ 已同时暂停跟单，/resume 恢复")
        elif cmd == "/recent":
            rows = self.store.recent_trades(10)
            await event.reply("\n".join(
                f"#{t.id} {datetime.fromtimestamp(t.created_ts):%m-%d %H:%M} {t.channel} {t.symbol} {t.side} "
                f"{t.status} pnl={t.pnl if t.pnl is None else round(t.pnl, 2)}" for t in rows) or "暂无记录")
        elif cmd == "/help":
            await event.reply(HELP)

    # ----------------------------------------------------------------- run
    async def sync_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.trader.sync()
            except Exception:
                log.exception("sync failed")

    async def run(self) -> None:
        await self.client.start(phone=self.s.phone or None)
        await self.trader.start()
        await self.resolve_channels()
        if not self.channel_ids:
            raise SystemExit("没有可监听的频道，请检查 config.yaml")

        self.client.add_event_handler(self.on_channel_message,
                                      events.NewMessage(chats=list(self.channel_ids)))
        self.client.add_event_handler(self.on_command,
                                      events.NewMessage(chats="me", pattern=r"^/\w+"))
        sync = asyncio.create_task(self.sync_loop())
        await self.notify(
            f"🤖 跟单机器人已启动 — {'🧪 模拟模式' if self.s.dry_run else '💰 实盘模式'}\n"
            f"交易所: {self.s.exchange_id}  频道: {', '.join(self.channel_ids.values())}\n"
            f"每单保证金 {self.s.risk.margin_usdt}U, 最大杠杆 {self.s.risk.max_leverage}x, "
            f"必须有止损: {self.s.risk.require_stop_loss}\n发送 /help 查看指令")
        try:
            await self.client.run_until_disconnected()
        finally:
            sync.cancel()
            await self.trader.close()


def _worth_llm(text: str) -> bool:
    """Cheap pre-filter so ads and chatter don't cost an API call."""
    return bool(re.search(r"[$#][A-Za-z]{2,}|[A-Z]{2,}\s*/?\s*USDT", text)) and bool(re.search(r"\d", text))


async def login(s: Settings) -> None:
    Path(s.session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(s.session_path, s.api_id, s.api_hash)
    await client.start(phone=s.phone or None)
    me = await client.get_me()
    print(f"登录成功: {me.first_name} (@{me.username}) — session 已保存到 {s.session_path}.session")
    await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--login", action="store_true", help="interactive Telegram login, then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    s = load_settings(args.config, args.env)
    if not s.api_id or not s.api_hash:
        raise SystemExit("请在 .env 里填写 TG_API_ID 和 TG_API_HASH（https://my.telegram.org 申请）")
    if args.login:
        asyncio.run(login(s))
        return
    bot = Bot(s)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: loop.create_task(bot.client.disconnect()))
        except NotImplementedError:
            pass
    loop.run_until_complete(bot.run())


if __name__ == "__main__":
    main()

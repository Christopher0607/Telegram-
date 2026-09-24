"""入口

  python main.py login        第一次用：登录监听频道的 Telegram 账号（输入验证码）
  python main.py check        检查交易所 / AI / Telegram / 通知机器人是否配置正确
  python main.py replay 30    拿每个频道最近 30 条消息测试 AI 识别效果（不下单）
  python main.py run          正式运行（docker compose up 默认就是这个）
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, utils
from telethon.tl.functions.channels import JoinChannelRequest

from config import ChannelCfg, Config, DATA_DIR
from db import DB
from engine import MODE_CN, SIDE_CN, Engine, MsgCtx, fmt
from exchange import Exchange
from notifier import Notifier
from signal_parser import SignalParser

log = logging.getLogger("main")


def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    f = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(f)
    fh = RotatingFileHandler(os.path.join(DATA_DIR, "bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(f)
    root.addHandler(sh)
    root.addHandler(fh)
    for noisy in ("telethon", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_client(cfg: Config) -> TelegramClient:
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        sys.exit("❌ .env 里缺少 TG_API_ID / TG_API_HASH（在 my.telegram.org 申请）")
    os.makedirs(DATA_DIR, exist_ok=True)
    return TelegramClient(os.path.join(DATA_DIR, "telegram"), cfg.tg_api_id, cfg.tg_api_hash)


async def resolve_channels(client, cfg: Config, join: bool = True) -> dict:
    """username → 频道实体；没加入的频道自动加入（收实时消息必须先加入）。返回 {peer_id: ChannelCfg}"""
    out = {}
    for ch in cfg.channels:
        if ch.mode == "off":
            continue
        try:
            ent = await client.get_entity(ch.username)
        except Exception as e:
            log.error("找不到频道 @%s：%s", ch.username, e)
            continue
        if join and getattr(ent, "left", False):
            try:
                await client(JoinChannelRequest(ent))
                log.info("已加入频道 %s", getattr(ent, "title", ch.username))
            except Exception as e:
                log.warning("加入频道 @%s 失败：%s", ch.username, e)
        ch.title = getattr(ent, "title", "") or ch.username
        out[utils.get_peer_id(ent)] = ch
    return out


async def build_ctx(ch: ChannelCfg, msg, edited: bool) -> MsgCtx | None:
    text = (msg.raw_text or "").strip()
    if not text:
        return None  # 纯图片/视频没有文字，识别不了
    reply_text = None
    if msg.reply_to_msg_id:
        try:
            r = await msg.get_reply_message()
            reply_text = (r.raw_text or "").strip() if r else None
        except Exception:
            pass
    date = msg.edit_date if (edited and msg.edit_date) else msg.date
    return MsgCtx(channel=ch, title=ch.title or ch.username, chat_id=msg.chat_id, msg_id=msg.id, date=date,
                  text=text[:3000], reply_to=msg.reply_to_msg_id, reply_text=reply_text,
                  forwarded=msg.fwd_from is not None, edited=edited)


def describe(a: dict) -> str:
    t = a["type"]
    who = a.get("symbol") or "(按回复关联)"
    if t == "open":
        if a.get("entry_low") or a.get("entry_high"):
            zone = f"{'限价' if a['entry_type'] == 'limit' else '市价'} {fmt(a.get('entry_low'))}~{fmt(a.get('entry_high'))}"
        else:
            zone = "市价"
        tps = "/".join(fmt(x) for x in a["take_profits"]) or "-"
        return f"开仓 {a['symbol']} {SIDE_CN[a['side']]}｜{zone}｜止损 {fmt(a.get('stop_loss'))}｜止盈 {tps}｜{a['confidence']}"
    if t == "close":
        return f"平仓 {who} {a['fraction'] * 100:.0f}%｜{a['confidence']}"
    if t == "move_sl":
        return f"移动止损 {who} → {'保本' if a['breakeven'] else fmt(a['price'])}｜{a['confidence']}"
    if t == "update_tp":
        return f"更新止盈 {who} → {'/'.join(fmt(x) for x in a['take_profits'])}｜{a['confidence']}"
    return str(a)


# ============================== 命令 ==============================
async def cmd_login(cfg: Config):
    client = make_client(cfg)
    await client.start(phone=cfg.tg_phone or (lambda: input("手机号（带国家码，如 +8613800000000）：")))
    me = await client.get_me()
    print(f"✅ 登录成功：{me.first_name}（id={me.id}），会话已保存在 data/ 目录")
    await client.disconnect()


async def cmd_check(cfg: Config):
    print("\n== 1. 交易所 ==")
    ex = Exchange(cfg)
    try:
        await ex.init()
        print(f"✅ 行情接口正常（{len(ex.ex.markets)} 个市场）")
        for b in ("BTC", "HYPE", "NIL"):
            s, scale = ex.resolve(b)
            print(f"   {b} → {s or '没有这个合约'}" + (f"（价格×{scale:g}）" if scale != 1 else ""))
        if ex.has_keys:
            eq, free = await ex.balance()
            print(f"✅ API Key 正常（{'统一账户 UTA' if ex.uta else '经典账户'}）：权益 {eq:.2f}U，可用 {free:.2f}U")
            pos = await ex.positions()
            print(f"   当前持仓：{', '.join(pos) or '无'}")
            if ex.uta and cfg.margin_mode == "isolated":
                print("   ⚠️ 统一账户程序无法切换逐仓，请在 Bitget App 里把合约设为逐仓")
        else:
            print("ℹ️ 没填 Bitget API Key：只能跑模拟盘")
    except Exception as e:
        print(f"❌ 交易所出错：{e}")
    finally:
        await ex.close()

    print("\n== 2. AI 解析 ==")
    parser = SignalParser(cfg)
    try:
        sample = "#HYPE 輕倉市價多\n96.4-96.8\n✅止盈：99.8-104-113\n❌止損：93.7"
        res = await parser.parse(MsgCtx(ChannelCfg("test"), "测试", 0, 0, datetime.now(timezone.utc), sample))
        print(f"✅ {cfg.llm_provider}/{cfg.llm_model} 正常：", "；".join(describe(a) for a in res["actions"]) or "（无动作）")
    except Exception as e:
        print(f"❌ AI 出错：{e}")
    finally:
        await parser.close()

    print("\n== 3. Telegram ==")
    client = make_client(cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("❌ 还没登录，先运行：python main.py login")
            return
        me = await client.get_me()
        print(f"✅ 监听账号：{me.first_name}（id={me.id}）")
        chans = await resolve_channels(client, cfg, join=False)
        for ch in cfg.channels:
            ok = any(c is ch for c in chans.values())
            print(f"   {'✅' if ok else '❌'} @{ch.username} {ch.title}｜模式：{cfg.mode_for(ch)}")
        print("\n== 4. 通知机器人 ==")
        if not cfg.tg_bot_token:
            print("ℹ️ 没填 TG_BOT_TOKEN：通知会发到监听账号的「收藏夹」，命令功能不可用")
            return
        n = Notifier(cfg.tg_bot_token, cfg.tg_owner_id or me.id)
        ok = await n.send("✅ 测试消息：通知通道正常")
        await n.close()
        print("✅ 机器人已给你发测试消息" if ok else "❌ 机器人发不出消息：先在 Telegram 里给你的机器人发一次 /start，再重试")
    finally:
        await client.disconnect()


async def cmd_replay(cfg: Config, n: int):
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("❌ 还没登录，先运行：python main.py login")
    ex = Exchange(cfg)
    await ex.init()
    parser = SignalParser(cfg)
    try:
        chans = await resolve_channels(client, cfg, join=False)
        for ch in chans.values():
            msgs = [m async for m in client.iter_messages(ch.username, limit=n)]
            print(f"\n===== {ch.title}（@{ch.username}）最近 {len(msgs)} 条 =====")
            n_open = n_ok = 0
            for m in reversed(msgs):
                ctx = await build_ctx(ch, m, False)
                if not ctx:
                    continue
                res = await parser.parse(ctx)
                head = ctx.text.replace("\n", " ")[:60]
                print(f"\n[{m.date.astimezone().strftime('%m-%d %H:%M')}] {head}")
                if not res["actions"]:
                    print(f"   → 无指令（{res['note']}）")
                for a in res["actions"]:
                    flag = ""
                    if a["type"] == "open":
                        n_open += 1
                        problems = []
                        if a["confidence"] != "high":
                            problems.append("不够明确")
                        sl_note = ""
                        if not a.get("stop_loss"):
                            if str(cfg.risk_for(ch)["fallback_sl_mode"]).lower() in ("atr", "pct"):
                                sl_note = "（没给止损，程序会补）"
                            else:
                                problems.append("没止损")
                        if a.get("symbol") and not ex.resolve(a["symbol"])[0]:
                            problems.append("Bitget 无此合约")
                        n_ok += not problems
                        flag = f"  ✓会跟{sl_note}" if not problems else f"  ✗会跳过：{'、'.join(problems)}"
                    print(f"   → {describe(a)}{flag}")
            print(f"\n小结：{n_open} 个开仓信号，其中 {n_ok} 个符合跟单条件（还要再过价格/盈亏比等实时检查）")
    finally:
        await parser.close()
        await ex.close()
        await client.disconnect()


async def cmd_run(cfg: Config):
    db = DB(os.path.join(DATA_DIR, "trader.db"))
    ex = Exchange(cfg)
    await ex.init()
    if cfg.live_trading and not ex.has_keys:
        log.error("live_trading 已打开但没填 Bitget API Key → 本次全部按模拟盘运行")
        cfg.live_trading = False
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("❌ Telegram 还没登录，先运行：python main.py login")
    me = await client.get_me()
    notifier = Notifier(cfg.tg_bot_token, cfg.tg_owner_id or me.id, client)
    parser = SignalParser(cfg)
    engine = Engine(cfg, db, ex, parser, notifier)
    chans = await resolve_channels(client, cfg, join=True)
    if not chans:
        sys.exit("❌ 没有可监听的频道，检查 config.yaml")

    # 每个频道一个队列：同一频道的消息严格按顺序处理，不同频道互不阻塞
    queues: dict[int, asyncio.Queue] = {}
    tasks: list[asyncio.Task] = []

    async def worker(q: asyncio.Queue):
        while True:
            ch, msg, edited = await q.get()
            try:
                ctx = await build_ctx(ch, msg, edited)
                if ctx:
                    await engine.handle_message(ctx)
            except Exception:
                log.exception("处理消息出错")

    def enqueue(event, edited: bool):
        ch = chans.get(event.chat_id)
        if not ch:
            return
        if event.chat_id not in queues:
            queues[event.chat_id] = asyncio.Queue()
            tasks.append(asyncio.create_task(worker(queues[event.chat_id])))
        queues[event.chat_id].put_nowait((ch, event.message, edited))

    async def on_new(event):
        enqueue(event, False)

    async def on_edit(event):
        enqueue(event, True)

    client.add_event_handler(on_new, events.NewMessage(chats=list(chans)))
    client.add_event_handler(on_edit, events.MessageEdited(chats=list(chans)))
    tasks.append(asyncio.create_task(engine.monitor_forever()))
    tasks.append(asyncio.create_task(notifier.command_loop(engine.handle_command)))

    modes = "\n".join(f"• {c.title}：{MODE_CN[cfg.mode_for(c)]}" for c in chans.values())
    await notifier.send(f"🚀 信号跟单已启动（监听账号 {me.first_name}）\n"
                        f"实盘总开关：{'开' if cfg.live_trading else '关，全部模拟'}\n{modes}\n发 /help 查看命令")
    log.info("开始监听 %d 个频道", len(chans))
    try:
        await client.run_until_disconnected()
    finally:
        for t in tasks:
            t.cancel()
        await ex.close()
        await parser.close()
        await notifier.close()


def main():
    setup_logging()
    cfg = Config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "login":
        asyncio.run(cmd_login(cfg))
    elif cmd == "check":
        asyncio.run(cmd_check(cfg))
    elif cmd == "replay":
        asyncio.run(cmd_replay(cfg, int(sys.argv[2]) if len(sys.argv) > 2 else 30))
    elif cmd == "run":
        asyncio.run(cmd_run(cfg))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

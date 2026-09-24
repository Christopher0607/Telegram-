"""通知与控制：通过你自己的 Telegram 机器人推送消息、接收 /status 等命令。

没配置机器人时，通知会发到监听账号的「收藏夹 Saved Messages」（不会响铃，命令也用不了）。
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, bot_token: str, owner_id: int, tg_client=None):
        self.token = bot_token
        self.owner_id = int(owner_id or 0)
        self.tg = tg_client
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=10.0))

    @property
    def api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def close(self):
        await self.http.aclose()

    async def send(self, text: str) -> bool:
        """发通知。通过机器人发送成功返回 True。"""
        text = text[:4000]
        log.info("通知: %s", text.replace("\n", " | ")[:300])
        if self.token and self.owner_id:
            try:
                r = await self.http.post(f"{self.api}/sendMessage", json={
                    "chat_id": self.owner_id, "text": text, "disable_web_page_preview": True})
                if r.status_code == 200:
                    return True
                log.warning("机器人发消息失败 %s：%s（先给机器人发一次 /start）", r.status_code, r.text[:200])
            except Exception as e:
                log.warning("机器人发消息异常：%s", e)
        if self.tg:
            try:
                await self.tg.send_message("me", text)
            except Exception as e:
                log.warning("发送到收藏夹失败：%s", e)
        return False

    async def command_loop(self, handler):
        """长轮询机器人消息；只响应 owner 本人发来的 / 命令。"""
        if not self.token or not self.owner_id:
            log.info("未配置 TG_BOT_TOKEN，命令功能关闭")
            return
        offset = None
        try:  # 丢弃启动前积压的旧命令
            r = await self.http.get(f"{self.api}/getUpdates", params={"offset": -1, "timeout": 0})
            res = r.json().get("result") or []
            if res:
                offset = res[-1]["update_id"] + 1
        except Exception as e:
            log.warning("getUpdates 初始化失败：%s", e)
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                r = await self.http.get(f"{self.api}/getUpdates", params=params)
                if r.status_code == 409:
                    log.warning("这个机器人 token 正被别的程序使用（409），请给本程序单独建一个机器人")
                    await asyncio.sleep(60)
                    continue
                for u in r.json().get("result") or []:
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    if (msg.get("from") or {}).get("id") != self.owner_id or not text.startswith("/"):
                        continue
                    try:
                        reply = await handler(text)
                    except Exception as e:
                        log.exception("命令处理出错")
                        reply = f"命令出错：{e}"
                    if reply:
                        await self.send(reply)
            except Exception as e:
                log.warning("命令轮询异常：%s", e)
                await asyncio.sleep(5)

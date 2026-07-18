from __future__ import annotations

import asyncio
import logging
import re
import random
import time
from collections import OrderedDict
from typing import Any

import aiohttp
import msgspec

from . import __version__
from .config import Config
from .models import CoreReceive, CoreSend, ReplyContext, Segment
from .qq_api import QQAPI

log = logging.getLogger(__name__)
AT_RE = re.compile(r"<@!?\d+>")


class Adapter:
    def __init__(self, config: Config):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.api: QQAPI | None = None
        self.core: aiohttp.ClientWebSocketResponse | None = None
        self.self_id = config.app_id
        self._contexts: OrderedDict[tuple[str, str], ReplyContext] = OrderedDict()
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._heartbeat_ack = asyncio.Event()
        self.qq_connected = False
        self.core_connected = False

    async def run(self) -> None:
        log.info("启动 gscore-qqofficial v%s (%s)", __version__, __file__)
        timeout = aiohttp.ClientTimeout(total=60)
        # aiodns may bypass the Windows/proxy DNS path and fail even when
        # socket.getaddrinfo works. ThreadedResolver follows the OS resolver.
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            self.session = session
            self.api = QQAPI(self.config, session)
            failures = 0
            while True:
                started = time.monotonic()
                try:
                    await self._run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if time.monotonic() - started > 300:
                        failures = 0
                    delay = min(60.0, 2**failures) + random.uniform(0, 1)
                    failures = min(failures + 1, 6)
                    log.exception("连接中断，%.1f 秒后重连", delay)
                    await asyncio.sleep(delay)

    async def _run_once(self) -> None:
        assert self.session and self.api
        gateway = self._resume_url or await self.api.gateway()
        async with self.session.ws_connect(self.config.core_ws_url, max_msg_size=64 << 20) as core, self.session.ws_connect(
            gateway, heartbeat=None, max_msg_size=16 << 20
        ) as qq:
            self.core = core
            self.core_connected = True
            self.qq_connected = True
            log.info("已连接 gscore 和 QQ Gateway")
            try:
                tasks = [asyncio.create_task(self._qq_loop(qq)), asyncio.create_task(self._core_loop())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
            finally:
                self.qq_connected = False
                self.core_connected = False
                self.core = None

    async def _qq_loop(self, qq: aiohttp.ClientWebSocketResponse) -> None:
        heartbeat_task: asyncio.Task[None] | None = None
        async for frame in qq:
            if frame.type != aiohttp.WSMsgType.TEXT:
                continue
            packet = msgspec.json.decode(frame.data)
            op = packet.get("op")
            if packet.get("s") is not None:
                self._sequence = packet["s"]
            if op == 10:
                interval = packet["d"]["heartbeat_interval"] / 1000
                heartbeat_task = asyncio.create_task(self._heartbeat(qq, interval))
                token = await self.api.access_token()  # type: ignore[union-attr]
                if self._session_id:
                    await qq.send_json({"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._sequence}})
                else:
                    intents = (1 << 25) | (1 << 30) | (1 << 12)
                    await qq.send_json({"op": 2, "d": {"token": f"QQBot {token}", "intents": intents, "shard": [0, 1], "properties": {"$os": "python", "$browser": "gscore-qq", "$device": "gscore-qq"}}})
            elif op == 0:
                if packet.get("t") == "READY":
                    self._session_id = packet["d"]["session_id"]
                    self._resume_url = packet["d"].get("resume_gateway_url")
                    self.self_id = packet["d"].get("user", {}).get("id", self.config.app_id)
                else:
                    await self._handle_event(packet.get("t", ""), packet.get("d", {}), packet.get("id", ""))
            elif op == 11:
                self._heartbeat_ack.set()
            elif op in {7, 9}:
                if op == 9:
                    self._session_id = self._resume_url = None
                break
        if heartbeat_task:
            heartbeat_task.cancel()

    async def _heartbeat(self, qq: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._heartbeat_ack.clear()
            await qq.send_json({"op": 1, "d": self._sequence})
            try:
                await asyncio.wait_for(self._heartbeat_ack.wait(), timeout=max(10, interval * 0.8))
            except asyncio.TimeoutError as exc:
                await qq.close()
                raise ConnectionError("QQ Gateway heartbeat ACK timeout") from exc

    async def _handle_event(self, event: str, data: dict[str, Any], event_id: str) -> None:
        mapping = {
            "GROUP_AT_MESSAGE_CREATE": ("group", "group_openid", "group"),
            "C2C_MESSAGE_CREATE": ("c2c", "author.id", "direct"),
            "AT_MESSAGE_CREATE": ("channel", "channel_id", "sub_channel"),
            "DIRECT_MESSAGE_CREATE": ("direct", "guild_id", "direct"),
        }
        if event not in mapping or not self.core:
            return
        kind, target_path, user_type = mapping[event]
        author = data.get("author", {})
        target_id = author.get("id", "") if target_path == "author.id" else data.get(target_path, "")
        user_id = author.get("member_openid") or author.get("user_openid") or author.get("id", "")
        text = AT_RE.sub("", data.get("content", "")).strip()
        content = [Segment("text", text)] if text else []
        for attachment in data.get("attachments", []):
            if attachment.get("url"):
                # MessageReceive expects a directly downloadable URL. link:// is
                # only the marker used by core when it sends images to adapters.
                content.append(Segment("image", attachment["url"]))
        if event in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}:
            content.append(Segment("at", self.self_id))
        msg_id = data.get("id", event_id)
        ctx = ReplyContext(kind, str(target_id), msg_id, event_id)
        # gscore addresses direct replies by user_id, while QQ channel DMs send to guild_id.
        context_id = str(user_id) if kind == "direct" else str(target_id)
        core_kind = "c2c" if kind == "direct" else kind
        key = (core_kind, context_id)
        self._contexts[key] = ctx
        self._contexts.move_to_end(key)
        while len(self._contexts) > 2048:
            self._contexts.popitem(last=False)
        receive = CoreReceive(
            bot_self_id=self.self_id, msg_id=msg_id, user_type=user_type, group_id=str(target_id) if user_type != "direct" else None,
            user_id=str(user_id), sender={"nickname": author.get("username") or data.get("member", {}).get("nick", "")}, content=content,
        )
        await self.core.send_bytes(msgspec.json.encode(receive))

    async def _core_loop(self) -> None:
        assert self.core and self.api
        async for frame in self.core:
            if frame.type not in {aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT}:
                continue
            message = msgspec.json.decode(frame.data, type=CoreSend)
            if message.bot_id != "qqofficial" or not message.target_id:
                continue
            kind = self._core_target_kind(message.target_type)
            ctx = self._contexts.get((kind, message.target_id))
            if not ctx:
                log.warning("找不到目标 %s:%s 的原消息，QQ 官方 API 不允许无上下文回复", kind, message.target_id)
                continue
            if time.monotonic() - ctx.created_at > 290:
                self._contexts.pop((kind, message.target_id), None)
                log.warning("目标 %s:%s 的回复上下文已过期", kind, message.target_id)
                continue
            try:
                await self._send_core_message(message, ctx, kind)
            except Exception:
                # A rejected/expired response must not tear down healthy WebSockets.
                log.exception("发送 QQ 消息失败，连接保持运行")

    async def _send_core_message(self, message: CoreSend, ctx: ReplyContext, kind: str) -> None:
        assert self.api
        text_parts: list[str] = []
        for segment in message.content or []:
            if segment.type == "text" and segment.data:
                text_parts.append(str(segment.data))
            elif segment.type == "at" and segment.data and kind in {"channel", "direct"}:
                text_parts.append(f"<@{segment.data}>")
            elif segment.type == "image" and segment.data:
                if text_parts:
                    await self.api.send_text(ctx, "".join(text_parts))
                    text_parts.clear()
                await self.api.send_image(ctx, str(segment.data))
        if text_parts:
            await self.api.send_text(ctx, "".join(text_parts))

    @staticmethod
    def _core_target_kind(target_type: str | None) -> str:
        return {"group": "group", "direct": "c2c", "sub_channel": "channel", "channel": "channel"}.get(target_type or "", target_type or "group")

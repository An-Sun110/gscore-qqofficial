from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import random
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any

import aiohttp
import msgspec

from . import __version__
from .config import Config
from .models import CoreReceive, CoreSend, ReplyContext, Segment
from .qq_api import QQAPI
from .state import StateStore

log = logging.getLogger(__name__)
AT_RE = re.compile(r"<@!?\d+>")
MSG_TYPE_QUOTE = 103


class Adapter:
    def __init__(self, config: Config):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.api: QQAPI | None = None
        self.store: StateStore | None = None
        self.self_id = config.app_id
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._heartbeat_ack = asyncio.Event()
        self._stop = asyncio.Event()
        self._core_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=config.queue_size)
        self._send_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.metrics: Counter[str] = Counter()
        self.qq_connected = False
        self.core_connected = False

    def request_stop(self) -> None:
        log.info("收到停止信号，正在关闭连接")
        self._stop.set()

    async def run(self) -> None:
        log.info("启动 gscore-qqofficial v%s (%s)", __version__, __file__)
        self.store = StateStore(self.config.state_path)
        await self.store.prune()
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            self.session = session
            self.api = QQAPI(self.config, session)
            tasks = [
                asyncio.create_task(self._qq_supervisor(), name="qq-supervisor"),
                asyncio.create_task(self._core_supervisor(), name="core-supervisor"),
                asyncio.create_task(self._metrics_loop(), name="metrics"),
            ]
            await self._stop.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()
        log.info("适配器已安全停止")

    async def _qq_supervisor(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._qq_session()
                if not self._stop.is_set():
                    raise ConnectionError("QQ Gateway closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.qq_connected = False
                self.metrics["qq_reconnects"] += 1
                failures = 0 if time.monotonic() - started > 300 else min(failures + 1, 6)
                delay = min(60.0, 2 ** max(0, failures - 1)) + random.uniform(0, 1)
                log.exception("QQ Gateway 中断，%.1f 秒后重连", delay)
                await self._wait_or_stop(delay)

    async def _qq_session(self) -> None:
        assert self.session and self.api
        gateway = self._resume_url or await self.api.gateway()
        async with self.session.ws_connect(gateway, heartbeat=None, max_msg_size=16 << 20) as qq:
            self.qq_connected = True
            log.info("已连接 QQ Gateway")
            await self._qq_loop(qq)
        self.qq_connected = False

    async def _core_supervisor(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                assert self.session
                async with self.session.ws_connect(self.config.core_ws_url, max_msg_size=64 << 20) as core:
                    self.core_connected = True
                    log.info("已连接 gsuid_core")
                    failures = 0
                    receive = asyncio.create_task(self._core_receive_loop(core))
                    send = asyncio.create_task(self._core_send_loop(core))
                    done, pending = await asyncio.wait((receive, send), return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        task.result()
                    if not self._stop.is_set():
                        raise ConnectionError("gsuid_core WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.core_connected = False
                self.metrics["core_reconnects"] += 1
                failures = 0 if time.monotonic() - started > 300 else min(failures + 1, 6)
                delay = min(60.0, 2 ** max(0, failures - 1)) + random.uniform(0, 1)
                log.exception("gsuid_core 中断，%.1f 秒后重连", delay)
                await self._wait_or_stop(delay)
            finally:
                self.core_connected = False

    async def _wait_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _qq_loop(self, qq: aiohttp.ClientWebSocketResponse) -> None:
        heartbeat_task: asyncio.Task[None] | None = None
        try:
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
                        try:
                            await self._handle_event(packet.get("t", ""), packet.get("d", {}), packet.get("id", ""))
                            self.metrics["events_ok"] += 1
                        except Exception:
                            self.metrics["events_failed"] += 1
                            log.exception("单条 QQ 事件处理失败 event=%s msg_id=%s", packet.get("t"), packet.get("d", {}).get("id"))
                elif op == 11:
                    self._heartbeat_ack.set()
                elif op in {7, 9}:
                    if op == 9:
                        self._session_id = self._resume_url = None
                    return
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _heartbeat(self, qq: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._heartbeat_ack.clear()
            await qq.send_json({"op": 1, "d": self._sequence})
            try:
                await asyncio.wait_for(self._heartbeat_ack.wait(), timeout=max(10, interval * 0.8))
            except asyncio.TimeoutError:
                log.error("QQ Gateway heartbeat ACK timeout")
                await qq.close()
                return

    async def _handle_event(self, event: str, data: dict[str, Any], event_id: str) -> None:
        mapping = {
            "GROUP_AT_MESSAGE_CREATE": ("group", "group_openid", "group"),
            "GROUP_MESSAGE_CREATE": ("group", "group_openid", "group"),
            "C2C_MESSAGE_CREATE": ("c2c", "author.id", "direct"),
            "AT_MESSAGE_CREATE": ("channel", "channel_id", "sub_channel"),
            "DIRECT_MESSAGE_CREATE": ("direct", "guild_id", "direct"),
        }
        if event not in mapping:
            return
        assert self.store
        kind, target_path, user_type = mapping[event]
        author = data.get("author", {})
        user_id = author.get("member_openid") or author.get("user_openid") or author.get("id", "")
        target_id = user_id if target_path == "author.id" else data.get(target_path, "")
        context_id = str(user_id) if kind in {"c2c", "direct"} else str(target_id)
        core_kind = "c2c" if kind == "direct" else kind
        text = AT_RE.sub("", data.get("content", "")).strip()
        content = [Segment("text", text)] if text else []
        image_urls = [item["url"] for item in data.get("attachments", []) if item.get("url")]
        ref_idx, msg_idx = self._parse_ref_indices(data)
        reference_id = (data.get("message_reference") or {}).get("message_id", "") or ref_idx
        if reference_id:
            content.append(Segment("reply", reference_id))
            quoted_urls = self._quoted_attachment_urls(data)
            if not quoted_urls:
                quoted_urls = await self.store.get_images(reference_id)
            for url in quoted_urls:
                if url not in image_urls:
                    image_urls.append(url)
        elif not image_urls and text:
            # QQ C2C/group events do not expose quote metadata. Associate a
            # following command with the latest image from the same session.
            image_urls.extend(await self.store.get_conversation_images(core_kind, context_id))
        content.extend(Segment("image", url) for url in image_urls)
        if event in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}:
            content.append(Segment("at", self.self_id))
        msg_id = data.get("id", event_id)
        if image_urls:
            await self.store.save_images(msg_id, image_urls)
            if msg_idx and msg_idx != msg_id:
                await self.store.save_images(msg_idx, image_urls)
            if data.get("attachments"):
                await self.store.save_conversation_images(core_kind, context_id, image_urls)
        ctx = ReplyContext(kind, str(target_id), msg_id, event_id)
        await self.store.save_context(core_kind, context_id, ctx)
        if await self._handle_admin_command(text, str(user_id), core_kind, context_id, ctx):
            return
        receive = CoreReceive(
            bot_self_id=self.self_id, msg_id=msg_id, user_type=user_type,
            group_id=str(target_id) if user_type != "direct" else None, user_id=str(user_id),
            sender={"nickname": author.get("username") or data.get("member", {}).get("nick", "")}, content=content,
        )
        payload = msgspec.json.encode(receive)
        try:
            self._core_queue.put_nowait(payload)
            self.metrics["events_queued"] += 1
        except asyncio.QueueFull:
            self.metrics["events_dropped"] += 1
            log.error("core 上报队列已满，丢弃消息 msg_id=%s", msg_id)

    async def _core_send_loop(self, core: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            payload = await self._core_queue.get()
            try:
                await core.send_bytes(payload)
                self.metrics["core_sent"] += 1
            except Exception:
                try:
                    self._core_queue.put_nowait(payload)
                except asyncio.QueueFull:
                    self.metrics["events_dropped"] += 1
                raise
            finally:
                self._core_queue.task_done()

    async def _core_receive_loop(self, core: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in core:
            if frame.type not in {aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT}:
                continue
            try:
                message = msgspec.json.decode(frame.data, type=CoreSend)
                await self._handle_core_message(message)
            except Exception:
                self.metrics["responses_failed"] += 1
                log.exception("单条 core 下发消息处理失败")

    async def _handle_core_message(self, message: CoreSend) -> None:
        if message.bot_id != "qqofficial" or not message.target_id:
            return
        assert self.store
        kind = self._core_target_kind(message.target_type)
        key = (kind, message.target_id)
        async with self._send_locks[key]:
            ctx = await self.store.get_context(*key)
            if not ctx:
                if not self.config.proactive_enabled:
                    log.warning("找不到回复上下文且主动消息已禁用 %s:%s", *key)
                    return
                ctx = ReplyContext(kind, message.target_id, "")
                self.metrics["proactive_attempts"] += 1
                log.info("发送主动消息 %s:%s", *key)
            await self._send_core_message(message, ctx, key)
            self.metrics["proactive_ok" if not ctx.msg_id else "responses_ok"] += 1

    async def _send_core_message(self, message: CoreSend, ctx: ReplyContext, key: tuple[str, str]) -> None:
        text_parts: list[str] = []
        for segment in message.content or []:
            if segment.type == "text" and segment.data:
                text_parts.append(str(segment.data))
            elif segment.type == "at" and segment.data and ctx.kind in {"channel", "direct"}:
                text_parts.append(f"<@{segment.data}>")
            elif segment.type == "image" and segment.data:
                if text_parts:
                    await self._send_text(ctx, key, "".join(text_parts))
                    text_parts.clear()
                await self._send_image(ctx, key, str(segment.data))
        if text_parts:
            await self._send_text(ctx, key, "".join(text_parts))

    async def _send_text(self, ctx: ReplyContext, key: tuple[str, str], text: str) -> None:
        assert self.store and self.api
        if ctx.msg_id:
            await self.store.reserve_sequence(*key, ctx)
        await self.api.send_text(ctx, text)

    async def _send_image(self, ctx: ReplyContext, key: tuple[str, str], image: str) -> None:
        assert self.store and self.api
        if ctx.msg_id:
            await self.store.reserve_sequence(*key, ctx)
        await self.api.send_image(ctx, image)

    async def _handle_admin_command(self, text: str, user_id: str, core_kind: str, context_id: str, ctx: ReplyContext) -> bool:
        command = text.strip().lower()
        actions = {
            "/重启": "restart",
            "/更新": "update",
            "/gscore-qq restart": "restart",
            "/gscore-qq update": "update",
        }
        action = actions.get(command)
        if action is None:
            return False
        key = (core_kind, context_id)
        if user_id not in self.config.admin_ids:
            log.warning("用户 %s 尝试执行管理命令: %s", user_id, command)
            await self._send_text(ctx, key, "无权执行该命令。")
            return True
        if action == "restart":
            await self._send_text(ctx, key, "适配器正在重启。")
            await asyncio.sleep(0.5)
            self._replace_process()
        project = Path(__file__).resolve().parent.parent
        if not (project / ".git").is_dir():
            await self._send_text(ctx, key, "当前不是 Git 源码部署。Docker 请在主机执行 docker compose up -d --build。")
            return True
        await self._send_text(ctx, key, "正在检查并安装更新，完成后自动重启。")
        try:
            await self._run_update(project)
        except Exception as exc:
            log.exception("在线更新失败")
            await self._send_text(ctx, key, f"更新失败：{str(exc)[:160]}")
            return True
        await self._send_text(ctx, key, "更新完成，适配器正在重启。")
        await asyncio.sleep(0.5)
        self._replace_process()
        return True

    async def _metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.metrics_interval)
            assert self.store
            await self.store.prune()
            contexts, images = await self.store.counts()
            log.info(
                "health qq=%s core=%s queue=%d/%d contexts=%d images=%d metrics=%s",
                self.qq_connected, self.core_connected, self._core_queue.qsize(), self.config.queue_size,
                contexts, images, dict(self.metrics),
            )

    @staticmethod
    async def _run_update(project: Path) -> None:
        for command in (["git", "pull", "--ff-only"], [sys.executable, "-m", "pip", "install", "-e", "."]):
            process = await asyncio.create_subprocess_exec(
                *command, cwd=project, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            output, _ = await process.communicate()
            if process.returncode:
                raise RuntimeError(f"{' '.join(command)}: {output.decode(errors='replace')[-500:]}")

    @staticmethod
    def _replace_process() -> None:
        os.execv(sys.executable, [sys.executable, "-m", "gscore_qq"])

    @staticmethod
    def _core_target_kind(target_type: str | None) -> str:
        return {"group": "group", "direct": "c2c", "sub_channel": "channel", "channel": "channel"}.get(target_type or "", target_type or "group")

    @staticmethod
    def _parse_ref_indices(data: dict[str, Any]) -> tuple[str, str]:
        ref_idx = ""
        msg_idx = ""
        ext = (data.get("message_scene") or {}).get("ext") or []
        for item in ext:
            if not isinstance(item, str):
                continue
            if item.startswith("ref_msg_idx="):
                ref_idx = item.removeprefix("ref_msg_idx=").strip()
            elif item.startswith("msg_idx="):
                msg_idx = item.removeprefix("msg_idx=").strip()
            elif item.startswith("refMsgIdx:"):
                ref_idx = item.removeprefix("refMsgIdx:").strip()
            elif item.startswith("msgIdx:"):
                msg_idx = item.removeprefix("msgIdx:").strip()
        elements = data.get("msg_elements") or []
        if data.get("message_type") == MSG_TYPE_QUOTE and elements:
            ref_idx = elements[0].get("msg_idx") or ref_idx
        return ref_idx, msg_idx

    @staticmethod
    def _quoted_attachment_urls(data: dict[str, Any]) -> list[str]:
        if data.get("message_type") != MSG_TYPE_QUOTE:
            return []
        elements = data.get("msg_elements") or []
        if not elements:
            return []
        return [item["url"] for item in elements[0].get("attachments") or [] if item.get("url")]

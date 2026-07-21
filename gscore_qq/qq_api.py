from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import aiohttp

from .config import Config
from .models import ReplyContext


class QQAPIError(RuntimeError):
    pass


class QQAPI:
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self.config = config
        self.session = session
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            async with self.session.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": self.config.app_id, "clientSecret": self.config.app_secret},
            ) as response:
                data = await response.json()
                if response.status >= 400 or "access_token" not in data:
                    raise QQAPIError(f"QQ token request failed ({response.status}): {data}")
            self._token = data["access_token"]
            self._expires_at = time.monotonic() + max(30, int(data.get("expires_in", 300)) - 60)
            return self._token

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(4):
            token = await self.access_token()
            headers.update({"Authorization": f"QQBot {token}", "X-Union-Appid": self.config.app_id})
            try:
                async with self.session.request(method, self.config.api_base + path, headers=headers, **kwargs) as response:
                    data = await response.json(content_type=None) if response.content_length != 0 else {}
                    if response.status == 401 and attempt == 0:
                        self._token = ""
                        continue
                    if response.status == 429 or response.status >= 500:
                        if attempt < 3:
                            delay = float(response.headers.get("Retry-After", 2**attempt))
                            await asyncio.sleep(min(delay, 30))
                            continue
                    if response.status >= 400:
                        raise QQAPIError(f"QQ API {method} {path} failed ({response.status}): {data}")
                    return data
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if attempt == 3:
                    raise QQAPIError(f"QQ API {method} {path} network failure") from exc
                await asyncio.sleep(2**attempt)
        raise QQAPIError(f"QQ API {method} {path} exhausted retries")

    async def gateway(self) -> str:
        return (await self.request("GET", "/gateway/bot"))["url"]

    async def send_text(self, ctx: ReplyContext, content: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content or " "}
        if ctx.msg_id:
            payload.update({"msg_id": ctx.msg_id, "msg_seq": ctx.seq})
        if ctx.kind in {"group", "c2c"}:
            payload["msg_type"] = 0
        if ctx.kind == "group":
            return await self.request("POST", f"/v2/groups/{ctx.target_id}/messages", json=payload)
        if ctx.kind == "c2c":
            return await self.request("POST", f"/v2/users/{ctx.target_id}/messages", json=payload)
        if ctx.kind == "channel":
            return await self.request("POST", f"/channels/{ctx.target_id}/messages", json=payload)
        if ctx.kind == "direct":
            return await self.request("POST", f"/dms/{ctx.target_id}/messages", json=payload)
        raise QQAPIError(f"Unsupported target kind: {ctx.kind}")

    async def send_image(self, ctx: ReplyContext, image: str) -> dict[str, Any]:
        if ctx.kind not in {"group", "c2c"}:
            # Channel APIs accept image URLs in markdown; keep the minimal adapter explicit.
            return await self.send_text(ctx, image[7:] if image.startswith("link://") else "[图片]")
        target = "groups" if ctx.kind == "group" else "users"
        file_payload: dict[str, Any] = {"srv_send_msg": False}
        if image.startswith("link://"):
            file_payload.update({"file_type": 1, "url": image[7:]})
        else:
            encoded = image[9:] if image.startswith("base64://") else image
            base64.b64decode(encoded, validate=True)
            file_payload.update({"file_type": 1, "file_data": encoded})
        media = await self.request("POST", f"/v2/{target}/{ctx.target_id}/files", json=file_payload)
        payload = {"msg_type": 7, "media": media}
        if ctx.msg_id:
            payload.update({"msg_id": ctx.msg_id, "msg_seq": ctx.seq})
        return await self.request("POST", f"/v2/{target}/{ctx.target_id}/messages", json=payload)

from __future__ import annotations

from typing import Any, Literal
import time

import msgspec


class Segment(msgspec.Struct):
    type: str | None = None
    data: Any = None


class CoreReceive(msgspec.Struct):
    bot_id: str = "qqofficial"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: str | None = None
    user_id: str = ""
    sender: dict[str, Any] = {}
    user_pm: int = 6
    content: list[Segment] = []


class CoreSend(msgspec.Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: str | None = None
    target_id: str | None = None
    content: list[Segment] | None = None
    echo: str | None = None


class ReplyContext(msgspec.Struct):
    kind: str
    target_id: str
    msg_id: str
    event_id: str = ""
    seq: int = 0
    created_at: float = msgspec.field(default_factory=time.time)

import aiohttp
import msgspec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gscore_qq.adapter import Adapter
from gscore_qq.config import Config
from gscore_qq.models import ReplyContext
from gscore_qq.state import StateStore


async def make_adapter(tmp_path: Path) -> Adapter:
    adapter = Adapter(Config("id", "secret", state_path=tmp_path / "state.db"))
    adapter.store = StateStore(adapter.config.state_path)
    return adapter


def test_core_url_adds_token():
    config = Config("id", "secret", "ws://localhost:8765/ws/QQOfficial?x=1", "hello world")
    assert config.core_ws_url == "ws://localhost:8765/ws/QQOfficial?x=1&token=hello+world"


def test_target_mapping():
    assert Adapter._core_target_kind("group") == "group"
    assert Adapter._core_target_kind("direct") == "c2c"
    assert Adapter._core_target_kind("sub_channel") == "channel"


def test_admin_ids_are_loaded(monkeypatch):
    monkeypatch.setenv("QQ_APP_ID", "id")
    monkeypatch.setenv("QQ_APP_SECRET", "secret")
    monkeypatch.setenv("QQ_ADMIN_IDS", " user-a, user-b ,, ")
    assert Config.from_env().admin_ids == {"user-a", "user-b"}


async def test_threaded_resolver_is_available():
    # Windows deployments must not depend on aiodns being able to reach DNS.
    assert isinstance(aiohttp.ThreadedResolver(), aiohttp.ThreadedResolver)


async def test_incoming_image_uses_downloadable_url(tmp_path):
    adapter = await make_adapter(tmp_path)
    url = "https://multimedia.nt.qq.com.cn/download?fileid=abc"
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {"id": "msg", "author": {"id": "user"}, "attachments": [{"url": url}]},
        "event",
    )
    payload = msgspec.json.decode(await adapter._core_queue.get())
    assert payload["content"] == [{"type": "image", "data": url}]
    await adapter.store.close()


async def test_quoted_image_is_restored_from_message_cache(tmp_path):
    adapter = await make_adapter(tmp_path)
    url = "https://multimedia.nt.qq.com.cn/download?fileid=quoted"
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {"id": "image-msg", "author": {"id": "user"}, "attachments": [{"url": url}]},
        "event-1",
    )
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "command-msg",
            "author": {"id": "user"},
            "content": "评分",
            "message_reference": {"message_id": "image-msg"},
        },
        "event-2",
    )
    await adapter._core_queue.get()
    payload = msgspec.json.decode(await adapter._core_queue.get())
    assert payload["content"] == [
        {"type": "text", "data": "评分"},
        {"type": "reply", "data": "image-msg"},
        {"type": "image", "data": url},
    ]
    await adapter.store.close()


async def test_c2c_command_uses_latest_session_image_without_quote_metadata(tmp_path):
    adapter = await make_adapter(tmp_path)
    url = "https://multimedia.nt.qq.com.cn/download?fileid=session"
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {"id": "image-msg", "author": {"user_openid": "user"}, "attachments": [{"url": url}]},
        "event-1",
    )
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {"id": "command-msg", "author": {"user_openid": "user"}, "content": "ww上传ams面板图"},
        "event-2",
    )
    await adapter._core_queue.get()
    payload = msgspec.json.decode(await adapter._core_queue.get())
    assert payload["user_id"] == "user"
    assert payload["content"] == [
        {"type": "text", "data": "ww上传ams面板图"},
        {"type": "image", "data": url},
    ]
    await adapter.store.close()


async def test_qq_quote_uses_fresh_msg_element_attachment(tmp_path):
    adapter = await make_adapter(tmp_path)
    fresh_url = "https://multimedia.nt.qq.com.cn/download?rkey=fresh"
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "quote-command",
            "author": {"user_openid": "user"},
            "content": "ww上传ams面板图",
            "message_type": 103,
            "message_scene": {"ext": ["ref_msg_idx=old-ref", "msg_idx=new-msg"]},
            "msg_elements": [
                {
                    "msg_idx": "authoritative-ref",
                    "content": "",
                    "attachments": [{"content_type": "image/png", "url": fresh_url}],
                }
            ],
        },
        "event",
    )
    payload = msgspec.json.decode(await adapter._core_queue.get())
    assert payload["content"] == [
        {"type": "text", "data": "ww上传ams面板图"},
        {"type": "reply", "data": "authoritative-ref"},
        {"type": "image", "data": fresh_url},
    ]
    await adapter.store.close()


async def test_quote_cache_uses_msg_idx_not_platform_message_id(tmp_path):
    adapter = await make_adapter(tmp_path)
    url = "https://multimedia.nt.qq.com.cn/download?rkey=cached"
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "platform-id",
            "author": {"user_openid": "user"},
            "attachments": [{"url": url}],
            "message_scene": {"ext": ["msg_idx=qq-index"]},
        },
        "event-1",
    )
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "command",
            "author": {"user_openid": "user"},
            "content": "评分",
            "message_type": 103,
            "message_scene": {"ext": ["ref_msg_idx=qq-index"]},
        },
        "event-2",
    )
    await adapter._core_queue.get()
    payload = msgspec.json.decode(await adapter._core_queue.get())
    assert {"type": "image", "data": url} in payload["content"]
    await adapter.store.close()


async def test_bad_gateway_event_does_not_stop_next_event(tmp_path):
    adapter = await make_adapter(tmp_path)
    adapter._handle_event = AsyncMock(side_effect=[ValueError("bad event"), None])

    class FakeQQ:
        def __init__(self):
            self.frames = iter(
                [
                    SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=msgspec.json.encode({"op": 0, "t": "A", "d": {"id": "1"}})),
                    SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=msgspec.json.encode({"op": 0, "t": "B", "d": {"id": "2"}})),
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration

    await adapter._qq_loop(FakeQQ())
    assert adapter._handle_event.await_count == 2
    assert adapter.metrics["events_failed"] == 1
    assert adapter.metrics["events_ok"] == 1
    await adapter.store.close()


async def test_context_and_sequence_survive_store_reopen(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    ctx = ReplyContext("c2c", "target", "msg")
    await store.save_context("c2c", "target", ctx)
    await store.reserve_sequence("c2c", "target", ctx)
    await store.close()
    reopened = StateStore(path)
    restored = await reopened.get_context("c2c", "target")
    assert restored is not None
    assert restored.msg_id == "msg"
    assert restored.seq == 1
    await reopened.close()


def test_request_stop_sets_shutdown_event():
    adapter = Adapter(Config("id", "secret"))
    adapter.request_stop()
    assert adapter._stop.is_set()


async def test_run_closes_cleanly_when_stop_requested(tmp_path):
    adapter = Adapter(Config("id", "secret", state_path=tmp_path / "state.db"))
    adapter.request_stop()
    await adapter.run()
    assert adapter.store is not None

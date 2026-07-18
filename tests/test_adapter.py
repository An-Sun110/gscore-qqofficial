import aiohttp
import msgspec

from gscore_qq.adapter import Adapter
from gscore_qq.config import Config


class FakeCore:
    def __init__(self):
        self.payload = None

    async def send_bytes(self, payload):
        self.payload = msgspec.json.decode(payload)


def test_core_url_adds_token():
    config = Config("id", "secret", "ws://localhost:8765/ws/QQOfficial?x=1", "hello world")
    assert config.core_ws_url == "ws://localhost:8765/ws/QQOfficial?x=1&token=hello+world"


def test_target_mapping():
    assert Adapter._core_target_kind("group") == "group"
    assert Adapter._core_target_kind("direct") == "c2c"
    assert Adapter._core_target_kind("sub_channel") == "channel"


async def test_threaded_resolver_is_available():
    # Windows deployments must not depend on aiodns being able to reach DNS.
    assert isinstance(aiohttp.ThreadedResolver(), aiohttp.ThreadedResolver)


async def test_incoming_image_uses_downloadable_url():
    adapter = Adapter(Config("id", "secret"))
    core = FakeCore()
    adapter.core = core
    url = "https://multimedia.nt.qq.com.cn/download?fileid=abc"
    await adapter._handle_event(
        "C2C_MESSAGE_CREATE",
        {"id": "msg", "author": {"id": "user"}, "attachments": [{"url": url}]},
        "event",
    )
    assert core.payload["content"] == [{"type": "image", "data": url}]

import aiohttp

from gscore_qq.adapter import Adapter
from gscore_qq.config import Config


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

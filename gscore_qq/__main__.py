import asyncio
import logging
import signal

from .adapter import Adapter
from .config import Config


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    adapter = Adapter(config)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, adapter.request_stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(adapter.request_stop))
        await adapter.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        adapter.request_stop()


if __name__ == "__main__":
    main()

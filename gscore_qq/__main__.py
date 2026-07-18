import asyncio
import logging

from .adapter import Adapter
from .config import Config


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(Adapter(config).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()


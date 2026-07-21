from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    gscore_url: str = "ws://127.0.0.1:8765/ws/QQOfficial"
    gscore_token: str = ""
    api_base: str = "https://api.sgroup.qq.com"
    log_level: str = "INFO"
    admin_ids: frozenset[str] = frozenset()
    state_path: Path = Path("data/state.db")
    queue_size: int = 1000
    metrics_interval: int = 60
    proactive_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        app_id = os.getenv("QQ_APP_ID", "").strip()
        app_secret = os.getenv("QQ_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise ValueError("QQ_APP_ID and QQ_APP_SECRET are required")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            gscore_url=os.getenv("GSCORE_URL", cls.gscore_url),
            gscore_token=os.getenv("GSCORE_TOKEN", ""),
            api_base=os.getenv("QQ_API_BASE", cls.api_base).rstrip("/"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            admin_ids=frozenset(x.strip() for x in os.getenv("QQ_ADMIN_IDS", "").split(",") if x.strip()),
            state_path=Path(os.getenv("STATE_PATH", "data/state.db")),
            queue_size=max(10, int(os.getenv("QUEUE_SIZE", "1000"))),
            metrics_interval=max(10, int(os.getenv("METRICS_INTERVAL", "60"))),
            proactive_enabled=os.getenv("PROACTIVE_ENABLED", "true").lower() not in {"0", "false", "no", "off"},
        )

    @property
    def core_ws_url(self) -> str:
        if not self.gscore_token:
            return self.gscore_url
        parts = urlsplit(self.gscore_url)
        query = dict(parse_qsl(parts.query))
        query["token"] = self.gscore_token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

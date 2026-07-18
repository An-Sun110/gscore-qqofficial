from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from .models import ReplyContext


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS contexts (
                core_kind TEXT NOT NULL, context_id TEXT NOT NULL,
                kind TEXT NOT NULL, target_id TEXT NOT NULL,
                msg_id TEXT NOT NULL, event_id TEXT NOT NULL,
                seq INTEGER NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (core_kind, context_id)
            );
            CREATE TABLE IF NOT EXISTS message_images (
                msg_id TEXT PRIMARY KEY, urls TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_images (
                core_kind TEXT NOT NULL, context_id TEXT NOT NULL,
                urls TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (core_kind, context_id)
            );
            """
        )
        self._db.commit()

    async def save_context(self, core_kind: str, context_id: str, ctx: ReplyContext) -> None:
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO contexts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (core_kind, context_id, ctx.kind, ctx.target_id, ctx.msg_id, ctx.event_id, ctx.seq, ctx.created_at),
            )
            self._db.commit()

    async def get_context(self, core_kind: str, context_id: str) -> ReplyContext | None:
        async with self._lock:
            row = self._db.execute(
                "SELECT * FROM contexts WHERE core_kind=? AND context_id=?", (core_kind, context_id)
            ).fetchone()
        if not row or time.time() - row["created_at"] > 290:
            return None
        return ReplyContext(row["kind"], row["target_id"], row["msg_id"], row["event_id"], row["seq"], row["created_at"])

    async def reserve_sequence(self, core_kind: str, context_id: str, ctx: ReplyContext) -> int:
        async with self._lock:
            row = self._db.execute(
                "SELECT seq FROM contexts WHERE core_kind=? AND context_id=?", (core_kind, context_id)
            ).fetchone()
            ctx.seq = (row["seq"] if row else ctx.seq) + 1
            self._db.execute(
                "UPDATE contexts SET seq=? WHERE core_kind=? AND context_id=?", (ctx.seq, core_kind, context_id)
            )
            self._db.commit()
            return ctx.seq

    async def save_images(self, msg_id: str, urls: list[str]) -> None:
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO message_images VALUES (?, ?, ?)", (msg_id, json.dumps(urls), time.time())
            )
            self._db.commit()

    async def get_images(self, msg_id: str) -> list[str]:
        async with self._lock:
            row = self._db.execute("SELECT urls, created_at FROM message_images WHERE msg_id=?", (msg_id,)).fetchone()
        return json.loads(row["urls"]) if row and time.time() - row["created_at"] < 1800 else []

    async def save_conversation_images(self, core_kind: str, context_id: str, urls: list[str]) -> None:
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO conversation_images VALUES (?, ?, ?, ?)",
                (core_kind, context_id, json.dumps(urls), time.time()),
            )
            self._db.commit()

    async def get_conversation_images(self, core_kind: str, context_id: str, max_age: int = 300) -> list[str]:
        async with self._lock:
            row = self._db.execute(
                "SELECT urls, created_at FROM conversation_images WHERE core_kind=? AND context_id=?",
                (core_kind, context_id),
            ).fetchone()
        return json.loads(row["urls"]) if row and time.time() - row["created_at"] < max_age else []

    async def prune(self) -> None:
        async with self._lock:
            self._db.execute("DELETE FROM contexts WHERE created_at < ?", (time.time() - 290,))
            self._db.execute("DELETE FROM message_images WHERE created_at < ?", (time.time() - 1800,))
            self._db.execute("DELETE FROM conversation_images WHERE created_at < ?", (time.time() - 300,))
            self._db.execute(
                "DELETE FROM message_images WHERE msg_id NOT IN (SELECT msg_id FROM message_images ORDER BY created_at DESC LIMIT 2048)"
            )
            self._db.commit()

    async def counts(self) -> tuple[int, int]:
        async with self._lock:
            contexts = self._db.execute("SELECT count(*) FROM contexts").fetchone()[0]
            images = self._db.execute("SELECT count(*) FROM message_images").fetchone()[0]
        return contexts, images

    async def close(self) -> None:
        async with self._lock:
            self._db.commit()
            self._db.close()

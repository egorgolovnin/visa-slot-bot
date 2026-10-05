"""FSM state in Postgres, so a deploy or restart never loses a half-filled form."""
from __future__ import annotations

import json
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS fsm (
    key        TEXT PRIMARY KEY,
    state      TEXT,
    data       JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _key(k: StorageKey) -> str:
    return f"{k.bot_id}:{k.chat_id}:{k.user_id}:{k.thread_id or ''}:{k.business_connection_id or ''}:{k.destiny}"


class PgStorage(BaseStorage):
    def __init__(self, db: Database):
        self.db = db

    async def init(self) -> None:
        await self.db.pool.execute(SCHEMA)

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self.db.pool.execute(
            """INSERT INTO fsm (key, state) VALUES ($1, $2)
               ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""", _key(key), value)

    async def get_state(self, key: StorageKey) -> str | None:
        return await self.db.pool.fetchval("SELECT state FROM fsm WHERE key = $1", _key(key))

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await self.db.pool.execute(
            """INSERT INTO fsm (key, data) VALUES ($1, $2::jsonb)
               ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
            _key(key), json.dumps(data, ensure_ascii=False, default=str))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        raw = await self.db.pool.fetchval("SELECT data FROM fsm WHERE key = $1", _key(key))
        if raw is None:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    async def close(self) -> None:
        pass

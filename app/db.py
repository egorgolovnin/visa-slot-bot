from __future__ import annotations

from datetime import date

import asyncpg

from .diff import Change
from .vfs import Slot

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      BIGINT PRIMARY KEY,
    name       TEXT,
    allowed    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS missions (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    offered      BOOLEAN,
    last_checked TIMESTAMPTZ,
    last_error   TEXT
);
CREATE TABLE IF NOT EXISTS current_slots (
    mission     TEXT NOT NULL,
    centre      TEXT NOT NULL,
    category    TEXT NOT NULL,
    earliest    DATE,
    raw_date    TEXT,
    vfs_updated TEXT,
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mission, centre, category)
);
CREATE TABLE IF NOT EXISTS slot_events (
    id          BIGSERIAL PRIMARY KEY,
    mission     TEXT NOT NULL,
    centre      TEXT NOT NULL,
    category    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    old_date    DATE,
    new_date    DATE,
    vfs_updated TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS slot_events_mission_at ON slot_events (mission, at DESC);
CREATE TABLE IF NOT EXISTS subscriptions (
    tg_id     BIGINT NOT NULL REFERENCES users (tg_id) ON DELETE CASCADE,
    mission   TEXT NOT NULL,
    date_from DATE,
    date_to   DATE,
    PRIMARY KEY (tg_id, mission)
);
"""


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # ---- users ----
    async def upsert_user(self, tg_id: int, name: str, allowed: bool | None = None) -> bool:
        row = await self.pool.fetchrow(
            """INSERT INTO users (tg_id, name, allowed) VALUES ($1, $2, COALESCE($3, FALSE))
               ON CONFLICT (tg_id) DO UPDATE SET name = EXCLUDED.name,
                   allowed = COALESCE($3, users.allowed)
               RETURNING allowed""",
            tg_id, name, allowed,
        )
        return row["allowed"]

    async def is_allowed(self, tg_id: int) -> bool:
        return bool(await self.pool.fetchval("SELECT allowed FROM users WHERE tg_id = $1", tg_id))

    async def set_allowed(self, tg_id: int, allowed: bool) -> str | None:
        return await self.pool.fetchval(
            "UPDATE users SET allowed = $2 WHERE tg_id = $1 RETURNING name", tg_id, allowed)

    async def users(self) -> list[asyncpg.Record]:
        return await self.pool.fetch("SELECT tg_id, name, allowed FROM users ORDER BY created_at")

    # ---- missions ----
    async def set_mission(self, code: str, name: str, offered: bool | None, error: str | None = None) -> None:
        await self.pool.execute(
            """INSERT INTO missions (code, name, offered, last_checked, last_error)
               VALUES ($1, $2, $3, now(), $4)
               ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, offered = EXCLUDED.offered,
                   last_checked = now(), last_error = EXCLUDED.last_error""",
            code, name, offered, error,
        )

    async def discovered(self) -> bool:
        return bool(await self.pool.fetchval("SELECT count(*) FROM missions WHERE offered IS NOT NULL"))

    async def offered_missions(self) -> list[tuple[str, str]]:
        rows = await self.pool.fetch("SELECT code, name FROM missions WHERE offered ORDER BY name")
        return [(r["code"], r["name"]) for r in rows]

    async def mission_name(self, code: str) -> str | None:
        return await self.pool.fetchval("SELECT name FROM missions WHERE code = $1", code)

    # ---- slots ----
    async def current(self, mission: str) -> dict[tuple[str, str], date | None]:
        rows = await self.pool.fetch(
            "SELECT centre, category, earliest FROM current_slots WHERE mission = $1", mission)
        return {(r["centre"], r["category"]): r["earliest"] for r in rows}

    async def save_fetch(self, mission: str, slots: list[Slot], changes: list[Change], vfs_updated: str) -> None:
        async with self.pool.acquire() as c, c.transaction():
            # categories VFS no longer returns have no date now
            await c.execute(
                """UPDATE current_slots SET earliest = NULL, raw_date = '', vfs_updated = $2, seen_at = now()
                   WHERE mission = $1 AND earliest IS NOT NULL
                     AND NOT ((centre, category) = ANY(SELECT * FROM unnest($3::text[], $4::text[])))""",
                mission, vfs_updated, [s.centre for s in slots], [s.category for s in slots])
            await c.executemany(
                """INSERT INTO current_slots (mission, centre, category, earliest, raw_date, vfs_updated, seen_at)
                   VALUES ($1, $2, $3, $4, $5, $6, now())
                   ON CONFLICT (mission, centre, category) DO UPDATE SET earliest = EXCLUDED.earliest,
                       raw_date = EXCLUDED.raw_date, vfs_updated = EXCLUDED.vfs_updated, seen_at = now()""",
                [(mission, s.centre, s.category, s.earliest, s.raw_date, vfs_updated) for s in slots],
            )
            if changes:
                await c.executemany(
                    """INSERT INTO slot_events (mission, centre, category, kind, old_date, new_date, vfs_updated)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    [(mission, ch.slot.centre, ch.slot.category, ch.kind, ch.old, ch.slot.earliest, vfs_updated)
                     for ch in changes],
                )

    async def status(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT m.code, m.name, s.centre, s.category, s.earliest, s.vfs_updated, s.seen_at
               FROM missions m LEFT JOIN current_slots s ON s.mission = m.code
               WHERE m.offered ORDER BY m.name, s.centre, s.category""")

    async def history(self, mission: str, limit: int = 15) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT centre, category, kind, old_date, new_date, at FROM slot_events
               WHERE mission = $1 ORDER BY at DESC LIMIT $2""", mission, limit)

    async def stats(self, days: int, tz: str) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT mission, EXTRACT(HOUR FROM at AT TIME ZONE $2)::int AS hour, count(*) AS n
               FROM slot_events WHERE kind IN ('opened', 'earlier') AND at > now() - make_interval(days => $1)
               GROUP BY mission, hour ORDER BY mission, n DESC""", days, tz)

    # ---- subscriptions ----
    async def toggle_sub(self, tg_id: int, mission: str) -> bool:
        deleted = await self.pool.fetchval(
            "DELETE FROM subscriptions WHERE tg_id = $1 AND mission = $2 RETURNING 1", tg_id, mission)
        if deleted:
            return False
        await self.pool.execute(
            "INSERT INTO subscriptions (tg_id, mission) VALUES ($1, $2)", tg_id, mission)
        return True

    async def user_subs(self, tg_id: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT mission, date_from, date_to FROM subscriptions WHERE tg_id = $1", tg_id)

    async def set_window(self, tg_id: int, mission: str, d_from: date | None, d_to: date | None) -> bool:
        res = await self.pool.execute(
            "UPDATE subscriptions SET date_from = $3, date_to = $4 WHERE tg_id = $1 AND mission = $2",
            tg_id, mission, d_from, d_to)
        return res.endswith("1")

    async def subscribers(self, mission: str, d: date) -> list[int]:
        rows = await self.pool.fetch(
            """SELECT s.tg_id FROM subscriptions s JOIN users u ON u.tg_id = s.tg_id
               WHERE s.mission = $1 AND u.allowed
                 AND (s.date_from IS NULL OR $2 >= s.date_from)
                 AND (s.date_to IS NULL OR $2 <= s.date_to)""", mission, d)
        return [r["tg_id"] for r in rows]

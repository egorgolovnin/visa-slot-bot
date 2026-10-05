from __future__ import annotations

from datetime import date, datetime

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
ALTER TABLE users ADD COLUMN IF NOT EXISTS username  TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS source    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS lang      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS banned    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_bot BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS missions (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    offered      BOOLEAN,
    last_checked TIMESTAMPTZ,
    last_error   TEXT
);
ALTER TABLE missions ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;
ALTER TABLE missions ADD COLUMN IF NOT EXISTS error_at   TIMESTAMPTZ;

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
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS user_events (
    id      BIGSERIAL PRIMARY KEY,
    tg_id   BIGINT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT,
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_events_at ON user_events (at DESC);
CREATE INDEX IF NOT EXISTS user_events_user ON user_events (tg_id, at DESC);
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
    async def touch_user(self, tg_id: int, name: str, username: str | None, lang: str | None,
                         allow_new: bool) -> tuple[bool, bool]:
        """Create or refresh a user. Returns (is_new, allowed)."""
        row = await self.pool.fetchrow(
            """INSERT INTO users (tg_id, name, username, lang, allowed, last_seen)
               VALUES ($1, $2, $3, $4, $5, now())
               ON CONFLICT (tg_id) DO UPDATE SET name = EXCLUDED.name, username = EXCLUDED.username,
                   lang = EXCLUDED.lang, last_seen = now(), blocked_bot = FALSE
               RETURNING (xmax = 0) AS is_new, allowed, banned""",
            tg_id, name, username, lang, allow_new,
        )
        return row["is_new"], row["allowed"] and not row["banned"]

    async def set_source(self, tg_id: int, source: str) -> None:
        await self.pool.execute(
            "UPDATE users SET source = $2 WHERE tg_id = $1 AND source IS NULL", tg_id, source[:64])

    async def is_allowed(self, tg_id: int) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT allowed AND NOT banned FROM users WHERE tg_id = $1", tg_id))

    async def is_banned(self, tg_id: int) -> bool:
        return bool(await self.pool.fetchval("SELECT banned FROM users WHERE tg_id = $1", tg_id))

    async def set_allowed(self, tg_id: int, allowed: bool) -> str | None:
        return await self.pool.fetchval(
            "UPDATE users SET allowed = $2 WHERE tg_id = $1 RETURNING name", tg_id, allowed)

    async def set_banned(self, tg_id: int, banned: bool) -> str | None:
        return await self.pool.fetchval(
            "UPDATE users SET banned = $2 WHERE tg_id = $1 RETURNING name", tg_id, banned)

    async def mark_blocked_bot(self, tg_id: int) -> None:
        await self.pool.execute("UPDATE users SET blocked_bot = TRUE WHERE tg_id = $1", tg_id)

    async def broadcast_targets(self) -> list[int]:
        rows = await self.pool.fetch(
            "SELECT tg_id FROM users WHERE allowed AND NOT banned AND NOT blocked_bot")
        return [r["tg_id"] for r in rows]

    async def log_event(self, tg_id: int, kind: str, payload: str | None = None) -> None:
        await self.pool.execute(
            "INSERT INTO user_events (tg_id, kind, payload) VALUES ($1, $2, $3)",
            tg_id, kind, (payload or "")[:200])

    # ---- analytics ----
    async def user_summary(self) -> asyncpg.Record:
        return await self.pool.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE created_at > now() - interval '1 day')   AS new_1d,
                   count(*) FILTER (WHERE created_at > now() - interval '7 days')  AS new_7d,
                   count(*) FILTER (WHERE created_at > now() - interval '30 days') AS new_30d,
                   count(*) FILTER (WHERE last_seen  > now() - interval '1 day')   AS active_1d,
                   count(*) FILTER (WHERE last_seen  > now() - interval '7 days')  AS active_7d,
                   count(*) FILTER (WHERE banned)       AS banned,
                   count(*) FILTER (WHERE blocked_bot)  AS blocked_bot,
                   count(*) FILTER (WHERE NOT allowed AND NOT banned) AS pending,
                   (SELECT count(DISTINCT tg_id) FROM subscriptions) AS with_subs
            FROM users""")

    async def top_sources(self, limit: int = 8) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT COALESCE(source, 'прямой заход') AS source, count(*) AS n,
                      count(*) FILTER (WHERE created_at > now() - interval '7 days') AS n_7d
               FROM users GROUP BY 1 ORDER BY n DESC LIMIT $1""", limit)

    async def subs_by_mission(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT m.name, count(s.tg_id) AS n FROM missions m
               LEFT JOIN subscriptions s ON s.mission = m.code WHERE m.offered
               GROUP BY m.name ORDER BY n DESC, m.name""")

    async def event_counts(self, days: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT kind, payload, count(*) AS n FROM user_events
               WHERE at > now() - make_interval(days => $1) AND kind IN ('command', 'alert')
               GROUP BY kind, payload ORDER BY n DESC LIMIT 15""", days)

    async def alerts_sent(self, days: int) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM user_events WHERE kind = 'alert' AND at > now() - make_interval(days => $1)", days)

    async def daily_new_users(self, days: int = 14) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT d::date AS day, count(u.tg_id) AS n
               FROM generate_series(current_date - ($1 - 1), current_date, interval '1 day') d
               LEFT JOIN users u ON u.created_at::date = d::date
               GROUP BY d ORDER BY d""", days)

    async def users_page(self, offset: int, limit: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT u.tg_id, u.name, u.username, u.source, u.allowed, u.banned, u.blocked_bot,
                      u.created_at, u.last_seen, count(s.mission) AS subs
               FROM users u LEFT JOIN subscriptions s ON s.tg_id = u.tg_id
               GROUP BY u.tg_id ORDER BY u.last_seen DESC NULLS LAST OFFSET $1 LIMIT $2""", offset, limit)

    async def user_card(self, tg_id: int) -> tuple[asyncpg.Record | None, list, list]:
        user = await self.pool.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)
        subs = await self.pool.fetch(
            """SELECT m.name, s.date_from, s.date_to FROM subscriptions s
               JOIN missions m ON m.code = s.mission WHERE s.tg_id = $1""", tg_id)
        events = await self.pool.fetch(
            "SELECT kind, payload, at FROM user_events WHERE tg_id = $1 ORDER BY at DESC LIMIT 12", tg_id)
        return user, subs, events

    # ---- missions ----
    async def set_mission(self, code: str, name: str, offered: bool | None, error: str | None = None) -> None:
        await self.pool.execute(
            """INSERT INTO missions (code, name, offered, last_checked, last_error)
               VALUES ($1, $2, $3, now(), $4)
               ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, offered = EXCLUDED.offered,
                   last_checked = now(), last_error = EXCLUDED.last_error""",
            code, name, offered, error,
        )

    async def last_discovery(self) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT max(last_checked) FROM missions WHERE offered IS NOT NULL AND code NOT IN ('deu', 'grc')")

    async def was_fetched(self, code: str) -> bool:
        return bool(await self.pool.fetchval("SELECT fetched_at IS NOT NULL FROM missions WHERE code = $1", code))

    async def mission_error(self, code: str, error: str) -> None:
        await self.pool.execute(
            "UPDATE missions SET last_error = $2, error_at = now() WHERE code = $1", code, error)

    async def offered_missions(self) -> list[tuple[str, str]]:
        rows = await self.pool.fetch("SELECT code, name FROM missions WHERE offered ORDER BY name")
        return [(r["code"], r["name"]) for r in rows]

    async def mission_name(self, code: str) -> str | None:
        return await self.pool.fetchval("SELECT name FROM missions WHERE code = $1", code)

    async def missions_health(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT code, name, fetched_at, error_at, last_error FROM missions WHERE offered ORDER BY name")

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
            await c.execute(
                "UPDATE missions SET fetched_at = now(), last_error = NULL WHERE code = $1", mission)

    async def status(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT m.code, m.name, m.fetched_at, m.last_error, s.centre, s.category, s.earliest,
                      s.vfs_updated, s.seen_at
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
               WHERE s.mission = $1 AND u.allowed AND NOT u.banned AND NOT u.blocked_bot
                 AND (s.date_from IS NULL OR $2 >= s.date_from)
                 AND (s.date_to IS NULL OR $2 <= s.date_to)""", mission, d)
        return [r["tg_id"] for r in rows]

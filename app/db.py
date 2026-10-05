from __future__ import annotations

import secrets
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

CREATE TABLE IF NOT EXISTS orders (
    id                BIGSERIAL PRIMARY KEY,
    tg_id             BIGINT NOT NULL REFERENCES users (tg_id),
    mission           TEXT NOT NULL,
    date_from         DATE,
    date_to           DATE,
    applicant         TEXT NOT NULL,           -- encrypted JSON
    status            TEXT NOT NULL DEFAULT 'new',
    price_eur         NUMERIC(10, 2),
    appointment       TEXT,
    confirmation_file TEXT,
    invoice_id        BIGINT,
    invoice_url       TEXT,
    paid_at           TIMESTAMPTZ,
    paid_asset        TEXT,
    paid_amount       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS destination TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS expired_notified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fee_eur NUMERIC(10, 2) NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fee_invoice_id BIGINT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fee_invoice_url TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fee_paid_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fee_paid TEXT;
CREATE TABLE IF NOT EXISTS support_map (
    admin_chat BIGINT NOT NULL,
    admin_msg  BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (admin_chat, admin_msg)
);
CREATE TABLE IF NOT EXISTS observations (
    id        BIGSERIAL PRIMARY KEY,
    mission   TEXT NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    available BOOLEAN NOT NULL,
    earliest  DATE,
    n_dates   INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS observations_mission_at ON observations (mission, at);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS age_min INT;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS times   TEXT;
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS visa_cases (
    id           BIGSERIAL PRIMARY KEY,
    tg_id        BIGINT,
    order_id     BIGINT,
    country      TEXT NOT NULL,
    submitted_on DATE,
    decided_on   DATE,
    outcome      TEXT NOT NULL,            -- approved / refused / pending
    visa_kind    TEXT,                     -- single / multi
    validity     TEXT,
    comment      TEXT,
    publish      BOOLEAN NOT NULL DEFAULT FALSE,
    status       TEXT NOT NULL DEFAULT 'review',   -- review / published / hidden
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS visa_cases_status ON visa_cases (status);
ALTER TABLE visa_cases ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'bot';
ALTER TABLE visa_cases ADD COLUMN IF NOT EXISTS external_id TEXT;
ALTER TABLE visa_cases ADD COLUMN IF NOT EXISTS extra JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS visa_cases_ext ON visa_cases (source, external_id) WHERE external_id IS NOT NULL;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS case_asked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS people INT NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS prices (
    mission   TEXT PRIMARY KEY,
    price_eur NUMERIC(10, 2) NOT NULL
);
ALTER TABLE prices ALTER COLUMN price_eur DROP NOT NULL;
ALTER TABLE prices ADD COLUMN IF NOT EXISTS fee_eur NUMERIC(10, 2);
"""

ACTIVE = ("new", "in_progress")           # bookable: fee (if any) is paid
OPEN_STATUSES = ("awaiting_fee", "new", "in_progress", "booked", "paid")


def _random_order_no() -> int:
    return 100000 + secrets.randbelow(900000)


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)
        await self.renumber_sequential_orders()

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
                   (SELECT count(DISTINCT tg_id) FROM orders) AS with_orders
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

    async def orders_by_destination(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT COALESCE(destination, mission) AS destination, count(*) AS n,
                      count(*) FILTER (WHERE status IN ('new', 'in_progress', 'booked')) AS open
               FROM orders WHERE status <> 'cancelled' GROUP BY 1 ORDER BY n DESC""")

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
                      u.created_at, u.last_seen, count(o.id) AS orders
               FROM users u LEFT JOIN orders o ON o.tg_id = u.tg_id
               GROUP BY u.tg_id ORDER BY u.last_seen DESC NULLS LAST OFFSET $1 LIMIT $2""", offset, limit)

    async def user_card(self, tg_id: int) -> tuple[asyncpg.Record | None, list, list]:
        user = await self.pool.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)
        subs = await self.pool.fetch(
            "SELECT id, destination, mission, status FROM orders WHERE tg_id = $1 ORDER BY id DESC LIMIT 10", tg_id)
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
            "SELECT max(last_checked) FROM missions WHERE offered IS NOT NULL AND code NOT IN ('deu', 'grc') AND code NOT LIKE '%-%'")

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

    # ---- orders ----
    async def create_order(self, tg_id: int, mission: str, d_from, d_to, applicant: str,
                           destination: str | None = None, fee: float = 0.0, people: int = 1) -> int:
        """Order numbers are random 6-digit values, so nobody can tell how many orders exist."""
        for _ in range(20):
            oid = await self.pool.fetchval(
                """INSERT INTO orders (id, tg_id, mission, date_from, date_to, applicant, destination, fee_eur, status, people)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) ON CONFLICT (id) DO NOTHING RETURNING id""",
                _random_order_no(), tg_id, mission, d_from, d_to, applicant, destination, fee,
                "awaiting_fee" if fee > 0 else "new", people)
            if oid:
                return oid
        raise RuntimeError("could not allocate an order number")

    async def renumber_sequential_orders(self) -> int:
        """One-off: orders created before random numbering get a random number too."""
        rows = await self.pool.fetch("SELECT id FROM orders WHERE id < 100000")
        for r in rows:
            for _ in range(20):
                new = _random_order_no()
                done = await self.pool.fetchval(
                    """UPDATE orders SET id = $2 WHERE id = $1
                       AND NOT EXISTS (SELECT 1 FROM orders WHERE id = $2) RETURNING id""", r["id"], new)
                if done:
                    break
        return len(rows)

    async def order(self, order_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """SELECT o.*, m.name AS mission_name, u.name AS user_name, u.username
               FROM orders o LEFT JOIN missions m ON m.code = o.mission
               LEFT JOIN users u ON u.tg_id = o.tg_id WHERE o.id = $1""", order_id)

    async def update_order(self, order_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        await self.pool.execute(
            f"UPDATE orders SET {cols}, updated_at = now() WHERE id = $1", order_id, *fields.values())

    async def orders_list(self, statuses: tuple[str, ...], tg_id: int | None = None, limit: int = 30) -> list:
        return await self.pool.fetch(
            """SELECT o.id, o.tg_id, o.mission, o.destination, o.status, o.people, o.date_from, o.date_to, o.price_eur, o.appointment,
                      o.invoice_url, o.created_at, o.fee_eur, o.fee_invoice_url, o.fee_paid_at, m.name AS mission_name, u.name AS user_name
               FROM orders o LEFT JOIN missions m ON m.code = o.mission LEFT JOIN users u ON u.tg_id = o.tg_id
               WHERE o.status = ANY($1::text[]) AND ($2::bigint IS NULL OR o.tg_id = $2)
               ORDER BY o.created_at DESC LIMIT $3""", list(statuses), tg_id, limit)

    async def open_order_missions(self, tg_id: int) -> set[str]:
        rows = await self.pool.fetch(
            """SELECT DISTINCT mission FROM orders WHERE tg_id = $1
               AND status IN ('awaiting_fee', 'new', 'in_progress', 'booked', 'paid')""", tg_id)
        return {r["mission"] for r in rows}

    async def active_orders_for(self, mission: str) -> list:
        return await self.pool.fetch(
            """SELECT id, date_from, date_to FROM orders
               WHERE mission = $1 AND status = ANY($2::text[]) ORDER BY created_at""", mission, list(ACTIVE))

    async def awaiting_payment(self) -> list:
        return await self.pool.fetch(
            """SELECT id, tg_id, invoice_id, expired_notified FROM orders
               WHERE status = 'booked' AND invoice_id IS NOT NULL""")

    async def orders_summary(self) -> asyncpg.Record:
        return await self.pool.fetchrow("""
            SELECT count(*) FILTER (WHERE status = 'awaiting_fee') AS awaiting_fee,
                   COALESCE(sum(fee_eur) FILTER (WHERE fee_paid_at IS NOT NULL), 0) AS fees_total,
                   count(*) FILTER (WHERE status = 'new')         AS new,
                   count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
                   count(*) FILTER (WHERE status = 'booked')      AS booked,
                   count(*) FILTER (WHERE status IN ('paid', 'done')) AS paid,
                   COALESCE(sum(price_eur) FILTER (WHERE status IN ('paid', 'done')
                            AND paid_at > now() - interval '30 days'), 0) AS revenue_30d,
                   COALESCE(sum(price_eur) FILTER (WHERE status IN ('paid', 'done')), 0) AS revenue_total
            FROM orders""")

    async def price(self, mission: str, default: float) -> float:
        v = await self.pool.fetchval("SELECT price_eur FROM prices WHERE mission = $1", mission)
        return float(v) if v is not None else default

    async def set_price(self, mission: str, price: float) -> None:
        await self.pool.execute(
            """INSERT INTO prices (mission, price_eur) VALUES ($1, $2)
               ON CONFLICT (mission) DO UPDATE SET price_eur = EXCLUDED.price_eur""", mission, price)

    async def fee(self, mission: str) -> float:
        from .requirements_by_country import requirements
        v = await self.pool.fetchval("SELECT fee_eur FROM prices WHERE mission = $1", mission)
        return float(v) if v is not None else requirements(mission).fee_at_booking

    async def set_fee(self, mission: str, fee: float) -> None:
        await self.pool.execute(
            """INSERT INTO prices (mission, fee_eur) VALUES ($1, $2)
               ON CONFLICT (mission) DO UPDATE SET fee_eur = EXCLUDED.fee_eur""", mission, fee)

    async def prices(self) -> list:
        return await self.pool.fetch(
            """SELECT m.code, m.name, p.price_eur, p.fee_eur FROM missions m LEFT JOIN prices p ON p.mission = m.code
               WHERE m.offered ORDER BY m.name""")

    async def awaiting_fee(self) -> list:
        return await self.pool.fetch(
            """SELECT id, tg_id, fee_invoice_id FROM orders
               WHERE status = 'awaiting_fee' AND fee_invoice_id IS NOT NULL""")

    # ---- support ----
    async def map_support(self, admin_chat: int, admin_msg: int, user_id: int) -> None:
        await self.pool.execute(
            "INSERT INTO support_map (admin_chat, admin_msg, user_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            admin_chat, admin_msg, user_id)

    async def support_user(self, admin_chat: int, admin_msg: int) -> int | None:
        return await self.pool.fetchval(
            "SELECT user_id FROM support_map WHERE admin_chat = $1 AND admin_msg = $2", admin_chat, admin_msg)

    async def support_count(self, days: int) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM user_events WHERE kind = 'support' AND at > now() - make_interval(days => $1)", days)

    async def purge_stale_forms(self, hours: int) -> None:
        """Half-filled forms hold passport data in plain JSON: drop abandoned ones."""
        await self.pool.execute(
            "DELETE FROM fsm WHERE updated_at < now() - make_interval(hours => $1)", hours)

    async def recent_events(self, tg_id: int, kind: str, minutes: int) -> int:
        return await self.pool.fetchval(
            """SELECT count(*) FROM user_events WHERE tg_id = $1 AND kind = $2
               AND at > now() - make_interval(mins => $3)""", tg_id, kind, minutes)

    # ---- slot statistics ----
    async def add_observation(self, mission: str, available: bool, earliest: date | None, n_dates: int,
                              age_min: int | None = None, times: str | None = None) -> None:
        await self.pool.execute(
            """INSERT INTO observations (mission, available, earliest, n_dates, age_min, times)
               VALUES ($1, $2, $3, $4, $5, $6)""", mission, available, earliest, n_dates, age_min, times)

    async def observations(self, days: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT o.mission, m.name, o.at, o.available, o.earliest, o.n_dates, o.age_min, o.times
               FROM observations o LEFT JOIN missions m ON m.code = o.mission
               WHERE o.at > now() - make_interval(days => $1) ORDER BY o.mission, o.at""", days)

    async def first_observation(self) -> datetime | None:
        return await self.pool.fetchval("SELECT min(at) FROM observations")

    async def kv_get(self, key: str) -> str | None:
        return await self.pool.fetchval("SELECT value FROM kv WHERE key = $1", key)

    async def kv_set(self, key: str, value: str) -> None:
        await self.pool.execute(
            "INSERT INTO kv (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value)

    async def event_total(self, kind: str, days: int) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM user_events WHERE kind = $1 AND at > now() - make_interval(days => $2)", kind, days)

    # ---- visa cases ----
    async def add_case(self, **f) -> int:
        cols = ", ".join(f)
        vals = ", ".join(f"${i + 1}" for i in range(len(f)))
        return await self.pool.fetchval(f"INSERT INTO visa_cases ({cols}) VALUES ({vals}) RETURNING id", *f.values())

    async def case(self, case_id: int):
        return await self.pool.fetchrow("SELECT * FROM visa_cases WHERE id = $1", case_id)

    async def set_case_status(self, case_id: int, status: str) -> None:
        await self.pool.execute("UPDATE visa_cases SET status = $2 WHERE id = $1", case_id, status)

    async def published_cases(self) -> list:
        return await self.pool.fetch(
            """SELECT country, submitted_on, decided_on, outcome, visa_kind, validity, comment, created_at
               FROM visa_cases WHERE status = 'published' ORDER BY COALESCE(decided_on, submitted_on, created_at::date) DESC""")

    async def orders_to_ask_case(self) -> list:
        return await self.pool.fetch(
            """SELECT id, tg_id, destination, mission, appointment FROM orders
               WHERE status IN ('paid', 'done') AND NOT case_asked AND appointment IS NOT NULL""")

    async def upsert_external_case(self, source: str, external_id: str, **f) -> bool:
        """Insert or refresh an imported case. Returns True if it is new."""
        exists = await self.pool.fetchval(
            "SELECT 1 FROM visa_cases WHERE source = $1 AND external_id = $2", source, external_id)
        cols = ["source", "external_id"] + list(f)
        vals = [source, external_id] + list(f.values())
        placeholders = ", ".join(f"${i + 1}" for i in range(len(vals)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in f)
        await self.pool.execute(
            f"""INSERT INTO visa_cases ({", ".join(cols)}) VALUES ({placeholders})
                ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL DO UPDATE SET {updates}""", *vals)
        return not exists

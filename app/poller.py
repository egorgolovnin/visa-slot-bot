from __future__ import annotations

import asyncio
import html
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .config import Settings
from .db import Database
from .diff import EARLIER, NOTIFY_KINDS, OPENED, Change, diff
from .vfs import SCHENGEN, Blocked, NotOffered, RateLimited, VfsPublicClient

log = logging.getLogger("poller")

Notify = Callable[[int, str], Awaitable[None]]

BLIND_AFTER = 3          # failed cycles in a row before the admins hear "bot is blind"
MAX_BACKOFF = 6          # max multiplier of the poll interval after 429s


@dataclass
class Health:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_cycle_at: datetime | None = None
    last_ok_at: datetime | None = None
    failures: int = 0
    last_error: str = ""
    backoff: int = 1
    blind: bool = False
    discovering: bool = False


def fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


class Poller:
    def __init__(self, settings: Settings, db: Database, notify: Notify):
        self.s = settings
        self.db = db
        self.notify = notify
        self.client = VfsPublicClient(settings.country, settings.proxy_url)
        self.health = Health()
        self._wake = asyncio.Event()

    async def alert_admins(self, text: str) -> None:
        for uid in self.s.admin_ids:
            await self.notify(uid, text)

    # ---- discovery: which Schengen missions does VFS serve in this country ----
    async def discover(self) -> str:
        self.health.discovering = True
        try:
            if self.s.missions:
                for code in self.s.missions:
                    await self.db.set_mission(code, SCHENGEN.get(code, code.upper()), True)
                return f"Миссии из MISSIONS: {', '.join(self.s.missions)}"
            found, blocked = [], None
            for code, name in SCHENGEN.items():
                try:
                    await self.client.fetch(code)
                    await self.db.set_mission(code, name, True)
                    found.append(name)
                    log.info("discover %s: offered", code)
                except NotOffered as e:
                    await self.db.set_mission(code, name, False)
                    log.info("discover %s: not offered (%s)", code, str(e)[:80])
                except Blocked as e:
                    blocked = str(e)
                    log.warning("discover %s: BLOCKED %s", code, blocked[:200])
                    break
                await asyncio.sleep(random.uniform(3, 6))
            log.info("discover done: found=%s blocked=%s", found, bool(blocked))
            if blocked:
                return f"Разведка прервана, VFS блокирует запросы: <code>{html.escape(blocked)}</code>"
            return "Найдено на VFS: " + (", ".join(found) if found else "ничего")
        finally:
            self.health.discovering = False

    # ---- one pass over all offered missions ----
    async def cycle(self) -> None:
        missions = await self.db.offered_missions()
        ok, errors = 0, []
        for code, name in missions:
            try:
                slots, vfs_updated = await self.client.fetch(code)
            except NotOffered:
                await self.db.set_mission(code, name, False, "not offered")
                continue
            except RateLimited as e:
                self.health.backoff = min(self.health.backoff * 2, MAX_BACKOFF)
                errors.append(f"{code}: 429")
                log.warning("rate limited on %s: %s", code, e)
                break
            except Blocked as e:
                errors.append(f"{code}: {e}")
                log.warning("blocked on %s: %s", code, e)
                continue
            ok += 1
            log.info("%s: %d pairs, %d with dates, vfs_updated=%s", code, len(slots),
                     sum(1 for x in slots if x.earliest), vfs_updated)
            prev = await self.db.current(code)
            changes = diff(prev, slots)
            first_sight = not prev
            await self.db.save_fetch(code, slots, changes, vfs_updated)
            if not first_sight:
                await self._announce(code, name, changes, vfs_updated)
            await asyncio.sleep(random.uniform(2, 5))

        h = self.health
        h.last_cycle_at = datetime.now(timezone.utc)
        if ok or not missions:
            h.last_ok_at, h.failures, h.last_error = h.last_cycle_at, 0, ""
            if not errors:
                h.backoff = 1
            if h.blind:
                h.blind = False
                await self.alert_admins("✅ Бот снова видит слоты.")
        else:
            h.failures += 1
            h.last_error = "; ".join(errors)[:500]
            if h.failures >= BLIND_AFTER and not h.blind:
                h.blind = True
                await self.alert_admins(
                    "⚠️ Бот ослеп: " + str(h.failures) + " цикла подряд без ответа VFS.\n"
                    f"<code>{html.escape(h.last_error[:300])}</code>\n"
                    "Если везде 403 — нужен резидентный прокси (PROXY_URL).")

    async def _announce(self, code: str, name: str, changes: list[Change], vfs_updated: str) -> None:
        for ch in changes:
            if ch.kind not in NOTIFY_KINDS or not ch.slot.earliest:
                continue
            head = "🟢 Появился слот" if ch.kind == OPENED else "⏫ Слот стал раньше"
            was = f" (было {fmt_date(ch.old)})" if ch.kind == EARLIER else ""
            text = (
                f"{head}: <b>{html.escape(name)}</b>\n"
                f"Центр: {html.escape(ch.slot.centre)}\n"
                f"Категория: {html.escape(ch.slot.category)}\n"
                f"Ближайшая дата: <b>{fmt_date(ch.slot.earliest)}</b>{was}\n"
                f"Данные VFS от: {html.escape(vfs_updated or '—')}\n"
                f"{self.client.page_url(code)}"
            )
            for uid in await self.db.subscribers(code, ch.slot.earliest):
                await self.notify(uid, text)

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        log.info("poller started, proxy=%s", bool(self.s.proxy_url))
        if not await self.db.discovered():
            result = await self.discover()
            await self.alert_admins("🔎 Первая разведка завершена.\n" + result)
        while True:
            try:
                await self.cycle()
            except Exception as e:  # keep the loop alive whatever happens
                log.exception("cycle crashed")
                self.health.last_error = f"crash: {e}"[:500]
            delay = self.s.poll_interval * self.health.backoff * random.uniform(0.85, 1.15)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

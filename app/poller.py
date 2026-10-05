from __future__ import annotations

import asyncio
import html
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from . import supersaas
from .config import Settings
from .db import Database
from .diff import EARLIER, NOTIFY_KINDS, OPENED, Change, diff
from .rktermin import RkTerminClient
from .supersaas import SuperSaasClient
from .vfs import SCHENGEN, Blocked, NotOffered, RateLimited, Slot, VfsPublicClient

log = logging.getLogger("poller")

Notify = Callable[[int, str], Awaitable[bool]]

FAIL_ALERT_AFTER = 3          # failed checks in a row before admins hear about one country
TRANSIENT_ALERT_AFTER = 6     # VFS's empty/1019/1020 answers come and go: wait ~1 hour before alerting
MAX_BACKOFF = 6               # max multiplier of the VFS poll interval after 429s
RK_COOLDOWN = 1800            # seconds to leave RK-Termin alone after a failed check
REALERT_AFTER = 1800          # same slot flapping open/closed: alert again only after this
DISCOVER_RETRIES = 3          # VFS answers transient 1019/1020 errors; retry before "not offered"
REDISCOVER_EVERY = timedelta(days=7)

# missions served by non-VFS systems: code -> display name
RK_MISSIONS = {"deu": "Германия (+NL, BE, DK, NO, IS, LU)"}
# VFS centres outside Cyprus (for clients who live there): code = "<vfs country>-<mission>"
ABROAD_MISSIONS = {"are-fra": "Франция (ОАЭ, Абу-Даби)", "are-ita": "Италия (ОАЭ, Дубай)",
                   "are-deu": "Германия (ОАЭ, Дубай)", "srb-hrv": "Хорватия (Сербия, Белград)"}
SS_MISSIONS = {"grc": "Греция (+Мальта)"}
NON_VFS = set(RK_MISSIONS) | set(SS_MISSIONS) | {"mlt", "nld", "bel", "dnk", "nor", "isl", "lux"}


@dataclass
class Health:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_cycle_at: datetime | None = None
    last_error: str = ""
    backoff: int = 1
    discovering: bool = False
    fails: dict[str, int] = field(default_factory=dict)      # consecutive failures per mission
    alerted: set[str] = field(default_factory=set)           # missions admins were warned about


def fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


class Poller:
    def __init__(self, settings: Settings, db: Database, notify: Notify):
        self.s = settings
        self.db = db
        self.notify = notify
        self.client = VfsPublicClient(settings.country, settings.proxy_url)
        self.rk = RkTerminClient(settings.proxy_url)
        self.rk_resume_at: datetime | None = None
        self.ss = SuperSaasClient(settings.proxy_url)
        self.health = Health()
        self._wake = asyncio.Event()
        self._last_alert: dict[tuple, datetime] = {}

    async def close(self) -> None:
        await self.client.close()
        await self.ss.close()

    async def alert_admins(self, text: str) -> None:
        for uid in self.s.admin_ids:
            await self.notify(uid, text)

    def page_url(self, code: str) -> str:
        if code in RK_MISSIONS:
            return self.rk.page_url()
        if code in SS_MISSIONS:
            return self.ss.page_url()
        return self.client.page_url(code)

    async def fetch(self, code: str) -> tuple[list[Slot], str]:
        if code in RK_MISSIONS:
            return await self.rk.fetch()
        if code in SS_MISSIONS:
            return await self.ss.fetch()
        return await self.client.fetch(code)

    async def register_static(self) -> None:
        for code, name in RK_MISSIONS.items():
            await self.db.set_mission(code, name, True)
        for code, name in ABROAD_MISSIONS.items():
            await self.db.set_mission(code, name, True)
        for code, name in SS_MISSIONS.items():
            await self.db.set_mission(code, name, supersaas.enabled())

    # ---- discovery: which Schengen missions does VFS serve in this country ----
    async def discover(self) -> str:
        self.health.discovering = True
        try:
            if self.s.missions:
                for code in self.s.missions:
                    await self.db.set_mission(code, SCHENGEN.get(code, code.upper()), True)
                await self.register_static()
                return f"Миссии из MISSIONS: {', '.join(self.s.missions)}"
            found, blocked = [], None
            for code, name in SCHENGEN.items():
                if code in NON_VFS:
                    continue
                offered = None
                for attempt in range(DISCOVER_RETRIES):
                    try:
                        await self.client.fetch(code)
                        offered = True
                        break
                    except NotOffered as e:
                        offered = False
                        log.info("discover %s try %d: not offered (%s)", code, attempt + 1, str(e)[:80])
                        await asyncio.sleep(random.uniform(2, 4))
                    except Blocked as e:
                        blocked = str(e)
                        break
                if blocked:
                    log.warning("discover %s: BLOCKED %s", code, blocked[:200])
                    break
                await self.db.set_mission(code, name, offered)
                if offered:
                    found.append(name)
                    log.info("discover %s: offered", code)
                await asyncio.sleep(random.uniform(3, 6))
            await self.register_static()
            log.info("discover done: found=%s blocked=%s", found, bool(blocked))
            if blocked:
                return f"Разведка прервана, VFS блокирует запросы: <code>{html.escape(blocked)}</code>"
            static = ", ".join(RK_MISSIONS.values()) + (", " + ", ".join(SS_MISSIONS.values()) if supersaas.enabled() else "")
            return "Найдено на VFS: " + (", ".join(found) if found else "ничего") + f"\nОтдельно: {static}"
        finally:
            self.health.discovering = False

    # ---- one pass over all offered missions ----
    async def cycle(self) -> None:
        missions = await self.db.offered_missions()
        now = datetime.now(timezone.utc)
        vfs_paused = False
        for code, name in missions:
            if code in RK_MISSIONS and self.rk_resume_at and now < self.rk_resume_at:
                continue
            is_vfs = code not in RK_MISSIONS and code not in SS_MISSIONS
            if is_vfs and vfs_paused:
                continue
            try:
                slots, updated = await self.fetch(code)
            except RateLimited as e:
                # a VFS 429 pauses only the other VFS countries, not Germany/Greece
                self.health.backoff = min(self.health.backoff * 2, MAX_BACKOFF)
                vfs_paused = True
                await self._failed(code, name, f"429: {e}")
                continue
            except NotOffered as e:
                # mid-cycle this is VFS's transient empty/1019/1020 answer: never disable a country here
                await self._failed(code, name, str(e), threshold=TRANSIENT_ALERT_AFTER)
                continue
            except Blocked as e:
                if code in RK_MISSIONS:
                    self.rk_resume_at = now + timedelta(seconds=RK_COOLDOWN)
                await self._failed(code, name, str(e))
                continue
            except Exception as e:  # one country must never break the whole cycle
                log.exception("unexpected error on %s", code)
                await self._failed(code, name, f"{type(e).__name__}: {e}")
                continue

            if code in RK_MISSIONS:
                self.rk_resume_at = None
                if self.rk.form_report:
                    report, self.rk.form_report = self.rk.form_report, None
                    await self.alert_admins("🔍 Германия: впервые увидел форму записи, ничего не отправлял.\n"
                                            + html.escape(report) + "\n\nПерешли это в чат с разработкой — допишем автобронь.")
            log.info("%s: %d pairs, %d with dates, updated=%s", code, len(slots),
                     sum(1 for x in slots if x.earliest), updated)
            first_sight = not await self.db.was_fetched(code)
            changes = diff(await self.db.current(code), slots)
            await self.db.save_fetch(code, slots, changes, updated)
            dated = [x.earliest for x in slots if x.earliest]
            n_dates = self.rk.last_free_days if code in RK_MISSIONS else len(dated)
            times = None
            if code in RK_MISSIONS and self.rk.last_times:
                # "2026-11-12=09:00,09:30;2026-11-13=10:00"
                times = ";".join(f"{d.isoformat()}={','.join(t)}" for d, t in sorted(self.rk.last_times.items()))
            age = self.client.last_age_min if is_vfs else None
            await self.db.add_observation(code, bool(dated), min(dated) if dated else None, n_dates, age, times)
            if not first_sight:
                await self._announce(code, name, changes, updated)
            await self._recovered(code, name)
            if is_vfs:
                await asyncio.sleep(random.uniform(2, 5))

        if not vfs_paused:
            self.health.backoff = 1
        self.health.last_cycle_at = datetime.now(timezone.utc)

    async def _failed(self, code: str, name: str, error: str, threshold: int = FAIL_ALERT_AFTER) -> None:
        h = self.health
        h.fails[code] = h.fails.get(code, 0) + 1
        h.last_error = f"{code}: {error}"[:500]
        log.warning("check failed %s (%d in a row): %s", code, h.fails[code], error[:200])
        await self.db.mission_error(code, error[:300])
        if h.fails[code] >= threshold and code not in h.alerted:
            h.alerted.add(code)
            hint = "\nЕсли везде 403 — нужен резидентный прокси (PROXY_URL)." if "403" in error else ""
            await self.alert_admins(f"⚠️ {html.escape(name)}: {h.fails[code]} проверки подряд с ошибкой.\n"
                                    f"<code>{html.escape(error[:300])}</code>{hint}")

    async def _recovered(self, code: str, name: str) -> None:
        h = self.health
        h.fails[code] = 0
        if code in h.alerted:
            h.alerted.discard(code)
            await self.alert_admins(f"✅ {html.escape(name)}: проверки снова работают.")

    async def _announce(self, code: str, name: str, changes: list[Change], updated: str) -> None:
        now = datetime.now(timezone.utc)
        for ch in changes:
            if ch.kind not in NOTIFY_KINDS or not ch.slot.earliest:
                continue
            key = (code, ch.slot.centre, ch.slot.category, ch.slot.earliest)
            last = self._last_alert.get(key)
            if last and (now - last).total_seconds() < REALERT_AFTER:
                log.info("skip repeated alert %s", key)
                continue
            self._last_alert[key] = now
            head = "🟢 Появился слот" if ch.kind == OPENED else "⏫ Слот стал раньше"
            was = f" (было {fmt_date(ch.old)})" if ch.kind == EARLIER else ""
            waiting = [o for o in await self.db.active_orders_for(code)
                       if (o["date_from"] is None or o["date_from"] <= ch.slot.earliest)
                       and (o["date_to"] is None or ch.slot.earliest <= o["date_to"])]
            ids = ", ".join(f"#{o['id']}" for o in waiting)
            orders_line = f"\n\n<b>Подходит заявкам: {ids}</b> → /orders" if waiting else "\n\nПодходящих заявок нет."
            times = self.rk.last_times.get(ch.slot.earliest, []) if code in RK_MISSIONS else []
            text = (
                f"{head}: <b>{html.escape(name)}</b>\n"
                f"Центр: {html.escape(ch.slot.centre)}\n"
                f"Категория: {html.escape(ch.slot.category)}\n"
                f"Ближайшая дата: <b>{fmt_date(ch.slot.earliest)}</b>{was}\n"
                + (f"Время: {', '.join(times)}\n" if times else "")
                + f"Данные от: {html.escape(updated or '—')}\n"
                f"{self.page_url(code)}{orders_line}"
            )
            await self.alert_admins(("⚡ " if waiting else "") + text)
            log.info("alert %s %s %s -> admins, %d matching orders", code, ch.kind, ch.slot.earliest, len(waiting))

    def wake(self) -> None:
        self._wake.set()

    async def probe_vfs_fields(self) -> None:
        """Once per deployment version: record the VFS booking-form configuration for each country."""
        from .vfs_fields import probe, summarize
        if await self.db.kv_get("vfs_fields_probe_abroad") == "v1":
            return
        await self.db.kv_set("vfs_fields_probe_abroad", "v1")
        for country, missions in (("are", ["fra", "ita", "deu"]), ("srb", ["hrv"])):
            await probe(country, missions, self.s.proxy_url)

    async def import_open_cases(self) -> None:
        """Once a day: refresh the open case base from cases.visarea.travel."""
        from datetime import date as _date
        from .import_cases import import_visarea
        today = "v4-" + _date.today().isoformat()  # bump the prefix to force a re-import after parser changes
        if await self.db.kv_get("visarea_import") == today:
            return
        try:
            summary = await import_visarea(self.db)
            first = await self.db.kv_get("visarea_import") is None
            await self.db.kv_set("visarea_import", today)
            if first:
                await self.alert_admins("📥 " + summary)
        except Exception as e:
            log.warning("visarea import failed: %s", e)

    async def run(self) -> None:
        log.info("poller started, proxy=%s", bool(self.s.proxy_url))
        await self.register_static()
        try:
            await self.probe_vfs_fields()
        except Exception:
            log.exception("vfs fields probe")
        await self.import_open_cases()
        last_discover = await self.db.last_discovery()
        if last_discover is None:
            await self.alert_admins("🔎 Первая разведка завершена.\n" + await self.discover())
        while True:
            try:
                await self.import_open_cases()
                last_discover = await self.db.last_discovery()
                if last_discover and datetime.now(timezone.utc) - last_discover > REDISCOVER_EVERY:
                    result = await self.discover()
                    log.info("weekly rediscovery: %s", result)
                await self.cycle()
            except Exception as e:
                log.exception("cycle crashed")
                self.health.last_error = f"crash: {e}"[:500]
            delay = self.s.poll_interval * self.health.backoff * random.uniform(0.85, 1.15)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

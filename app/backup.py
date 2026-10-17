"""Daily off-site backup of the database to the admin Telegram chat.

Dumps the valuable tables to CSV with Postgres COPY (no pg_dump binary needed, so no image
rebuild), packs them into one .tar.gz, and sends it as a document. The VPS disk is the only copy
otherwise — observations/slot_events (the whole "when do slots open" history), orders and users
would all be lost if the box dies.
"""
from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from datetime import date

log = logging.getLogger("backup")

TABLES = ["users", "missions", "observations", "slot_events", "subscriptions",
          "orders", "visa_cases", "direct_payments", "discounts", "prices", "kv"]


async def _dump(db) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        async with db.pool.acquire() as c:
            for t in TABLES:
                out = io.BytesIO()
                try:
                    await c.copy_from_query(f"SELECT * FROM {t}", output=out, format="csv", header=True)
                except Exception as e:
                    log.warning("backup: dump %s failed: %s", t, e)
                    continue
                data = out.getvalue()
                info = tarfile.TarInfo(name=f"{t}.csv")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def run_backup(db, bot, settings) -> int:
    """Build the archive and send it to each admin target. Returns the archive size in bytes."""
    from aiogram.types import BufferedInputFile
    data = await _dump(db)
    fname = f"visabot-backup-{date.today().isoformat()}.tar.gz"
    caption = f"🗄 Бэкап БД {date.today().isoformat()} ({max(1, len(data) // 1024)} КБ)"
    for uid in settings.admin_targets:
        try:
            await bot.send_document(uid, BufferedInputFile(data, filename=fname), caption=caption)
        except Exception as e:
            log.warning("backup: send to %s failed: %s", uid, e)
    log.info("backup sent: %d bytes", len(data))
    return len(data)


async def backup_watcher(bot, db, settings) -> None:
    """Fire once per day (gated by a kv key), checking hourly so a restart can't skip a day."""
    await asyncio.sleep(120)  # let startup settle
    while True:
        try:
            today = date.today().isoformat()
            if await db.kv_get("db_backup") != today:
                await db.kv_set("db_backup", today)
                await run_backup(db, bot, settings)
        except Exception:
            log.exception("backup watcher")
        await asyncio.sleep(3600)

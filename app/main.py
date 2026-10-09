import asyncio
import html
import logging
import time
import traceback

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ErrorEvent

from . import config
from . import cases, mailbox, orders, promo, support, web
from .bot import TrackingMiddleware, attach_access, router, setup_commands
from .db import Database
from .fsm_storage import PgStorage
from .poller import Poller

log = logging.getLogger("main")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = config.load()
    db = Database(settings.database_url)
    await db.init()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                               link_preview_is_disabled=True))

    async def notify(uid: int, text: str) -> bool:
        for _ in range(3):
            try:
                await bot.send_message(uid, text)
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await db.mark_blocked_bot(uid)  # user blocked the bot or never started it
                return False
            except Exception:
                log.exception("notify %s failed", uid)
                return False
        return False

    async def alert_admins(text: str) -> None:
        for uid in settings.admin_targets:
            await notify(uid, text)

    # Global handler: any unhandled exception in a user interaction pings the admin group,
    # throttled so one recurring bug can't spam. This is how we hear about bugs before clients do.
    _err_seen: dict[str, float] = {}

    async def on_error(event: ErrorEvent) -> bool:
        exc = event.exception
        tb = traceback.extract_tb(exc.__traceback__)
        where = f"{tb[-1].filename.split('/')[-1]}:{tb[-1].lineno}" if tb else "?"
        sig = f"{type(exc).__name__}@{where}"
        now = time.monotonic()
        last = _err_seen.get(sig, 0)
        _err_seen[sig] = now
        log.exception("handler error (%s)", sig)
        if now - last < 600:  # same error within 10 min — already reported
            return True
        upd = event.update
        ctx, user = "", None
        if getattr(upd, "message", None):
            user = upd.message.from_user
            if upd.message.text:
                ctx = f"\nСообщение: <code>{html.escape(upd.message.text[:80])}</code>"
        elif getattr(upd, "callback_query", None):
            user = upd.callback_query.from_user
            ctx = f"\nКнопка: <code>{html.escape((upd.callback_query.data or '')[:80])}</code>"
        uid = user.id if user else "?"
        await alert_admins(f"🐞 Ошибка у пользователя <code>{uid}</code> ({html.escape(sig)}):\n"
                           f"<code>{html.escape(str(exc)[:300])}</code>{ctx}")
        return True

    poller = Poller(settings, db, notify)
    orders._DB_FOR_STEPS.append(db)
    storage = PgStorage(db)
    await storage.init()
    dp = Dispatcher(storage=storage, db=db, settings=settings, poller=poller)
    dp.errors.register(on_error)
    dp.message.outer_middleware(TrackingMiddleware())
    dp.callback_query.outer_middleware(TrackingMiddleware())
    for r in (support.router, cases.router, promo.router, orders.router, router):  # state handlers first, catch-all last
        attach_access(r)
        dp.include_router(r)

    await setup_commands(bot, settings)
    if mailbox.enabled():
        try:
            log.info("mailbox ok: %s, %d messages in inbox", mailbox.address(), await mailbox.check())
        except Exception as e:
            log.error("mailbox check failed: %s", e)
    if orders.payments.enabled():
        try:
            log.info("crypto pay ok: %s", await orders.payments.check())
        except Exception as e:
            log.error("crypto pay check failed: %s", e)
    web_runner = await web.start(db, settings, poller, bot)
    task = asyncio.create_task(poller.run())
    wd_task = asyncio.create_task(poller.watchdog())
    pay_task = asyncio.create_task(orders.payment_watcher(bot, db, settings))
    try:
        await alert_admins("🔄 Easy Visa запущен. Если это не плановый деплой — бот перезапускался сам (падал).")
    except Exception:
        log.exception("startup ping failed")
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        wd_task.cancel()
        if web_runner:
            await web_runner.cleanup()
        pay_task.cancel()
        await poller.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

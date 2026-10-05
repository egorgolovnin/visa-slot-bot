import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from . import config
from . import cases, mailbox, orders, support, web
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

    poller = Poller(settings, db, notify)
    storage = PgStorage(db)
    await storage.init()
    dp = Dispatcher(storage=storage, db=db, settings=settings, poller=poller)
    dp.message.outer_middleware(TrackingMiddleware())
    dp.callback_query.outer_middleware(TrackingMiddleware())
    for r in (support.router, cases.router, orders.router, router):  # state handlers first, catch-all last
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
    web_runner = await web.start(db, settings, poller)
    task = asyncio.create_task(poller.run())
    pay_task = asyncio.create_task(orders.payment_watcher(bot, db, settings))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        if web_runner:
            await web_runner.cleanup()
        pay_task.cancel()
        await poller.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

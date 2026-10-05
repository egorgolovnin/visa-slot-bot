import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from . import config
from .bot import router
from .db import Database
from .poller import Poller

log = logging.getLogger("main")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = config.load()
    db = Database(settings.database_url)
    await db.init()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                               link_preview_is_disabled=True))

    async def notify(uid: int, text: str) -> None:
        for _ in range(3):
            try:
                await bot.send_message(uid, text)
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                return  # user blocked the bot
            except Exception:
                log.exception("notify %s failed", uid)
                return

    poller = Poller(settings, db, notify)
    dp = Dispatcher(db=db, settings=settings, poller=poller)
    dp.include_router(router)

    task = asyncio.create_task(poller.run())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await poller.client.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

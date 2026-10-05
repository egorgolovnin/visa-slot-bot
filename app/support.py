"""Anonymous support: clients write to the bot, admins answer through the bot. Nobody sees the admin's account."""
from __future__ import annotations

import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .db import Database
from .sanitize import clean_file

log = logging.getLogger("support")
router = Router()
NOT_A_COMMAND = ~F.text.startswith("/")


class Support(StatesGroup):
    writing = State()


class SupportReply(StatesGroup):
    writing = State()


def is_admin(uid: int, settings: Settings) -> bool:
    return uid in settings.admin_ids


async def ask_question(message: Message, state: FSMContext) -> None:
    await state.set_state(Support.writing)
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="sup:cancel")
    await message.answer("Напиши вопрос одним сообщением — можно с фото или файлом. Ответим здесь, в боте.",
                         reply_markup=kb.as_markup())


@router.message(Command("support"))
async def support_cmd(message: Message, state: FSMContext, settings: Settings) -> None:
    if is_admin(message.from_user.id, settings):
        await message.answer("Ты админ 🙂 Вопросы клиентов приходят тебе сюда. Ответить — «Ответить» под вопросом "
                             "или свайп-ответ (reply) на сообщение клиента.")
        return
    await ask_question(message, state)


@router.callback_query(F.data == "sup:cancel")
async def support_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.edit_text("Отменено.")
    except Exception:
        pass
    await call.answer()


@router.message(Support.writing, NOT_A_COMMAND)
async def support_message(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.clear()
    u = message.from_user
    if await db.recent_events(u.id, "support", 60) >= 5:
        await message.answer("Ты уже написал несколько сообщений за последний час — мы их видим и ответим. "
                             "Пожалуйста, подожди ответа 🙏")
        return
    orders = await db.orders_list(("new", "in_progress", "booked", "paid", "done"), tg_id=u.id, limit=5)
    order_line = ", ".join(f"#{o['id']} {o['status']}" for o in orders) or "заявок нет"
    head = (f"✉️ <b>Вопрос в поддержку</b>\nОт: {html.escape(u.full_name)}"
            + (f" @{html.escape(u.username)}" if u.username else "") + f" (<code>{u.id}</code>)\nЗаявки: {order_line}")
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Ответить", callback_data=f"sup:reply:{u.id}")
    delivered = False
    for admin_id in settings.admin_ids:
        try:
            h = await message.bot.send_message(admin_id, head, reply_markup=kb.as_markup())
            c = await message.bot.copy_message(admin_id, message.chat.id, message.message_id,
                                               reply_to_message_id=h.message_id)
            await db.map_support(admin_id, h.message_id, u.id)
            await db.map_support(admin_id, c.message_id, u.id)
            delivered = True
        except Exception:
            log.exception("support to admin %s failed", admin_id)
    await db.log_event(u.id, "support", (message.text or message.caption or "[media]")[:100])
    await message.answer("✅ Передали. Ответим здесь, в боте." if delivered
                         else "Не получилось передать вопрос, попробуй ещё раз чуть позже.")


# ---------------- admin side ----------------
async def deliver_reply(bot: Bot, admin_msg: Message, user_id: int, order_hint: str = "") -> None:
    """Send the admin's answer to the client as the bot. Documents are cleaned of metadata."""
    await bot.send_message(user_id, "💬 <b>Ответ поддержки</b>" + order_hint)
    if admin_msg.document:
        f = await bot.download(admin_msg.document.file_id)
        data, name = clean_file(f.read(), admin_msg.document.mime_type, admin_msg.document.file_name, "document")
        await bot.send_document(user_id, BufferedInputFile(data, filename=name), caption=admin_msg.caption)
    else:
        await bot.copy_message(user_id, admin_msg.chat.id, admin_msg.message_id)


@router.callback_query(F.data.startswith("sup:reply:"))
async def reply_button(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not is_admin(call.from_user.id, settings):
        await call.answer("Только для админа", show_alert=True)
        return
    uid = int(call.data.split(":")[2])
    await state.set_state(SupportReply.writing)
    await state.update_data(reply_to=uid)
    await call.message.answer(f"Пиши ответ для <code>{uid}</code> — текст, фото или файл. /cancel — отмена")
    await call.answer()


@router.message(SupportReply.writing, NOT_A_COMMAND)
async def reply_from_state(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    uid = (await state.get_data()).get("reply_to")
    await state.clear()
    await _send(message, db, uid)


@router.message(F.reply_to_message, NOT_A_COMMAND)
async def reply_by_swipe(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    """Admin answers with Telegram's reply on a forwarded question."""
    if not is_admin(message.from_user.id, settings):
        return await _not_handled(message, state)
    uid = await db.support_user(message.chat.id, message.reply_to_message.message_id)
    if uid is None:
        return await _not_handled(message, state)
    await _send(message, db, uid)


async def _not_handled(message: Message, state: FSMContext):
    from aiogram.dispatcher.event.bases import SkipHandler
    raise SkipHandler()  # not a support reply: let the other handlers see this message


async def _send(message: Message, db: Database, uid: int | None) -> None:
    if not uid:
        await message.answer("Не понял, кому отвечать.")
        return
    try:
        await deliver_reply(message.bot, message, uid)
        await db.log_event(uid, "support_answer", (message.text or message.caption or "[media]")[:100])
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        log.exception("reply to %s failed", uid)
        await message.answer(f"Не доставлено: {html.escape(str(e))[:200]}")

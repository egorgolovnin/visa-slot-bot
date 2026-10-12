"""Referral programme and fortune wheel."""
from __future__ import annotations

import asyncio
import html
import random

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .db import Database

router = Router()

# fortune wheel: weighted sectors (percent discount -> weight). One spin per user.
WHEEL = [(5, 34), (10, 30), (15, 20), (20, 12), (30, 3), (100, 1)]  # 100% is the rare jackpot
WHEEL_LABELS = [f"{p}%" for p, _ in WHEEL]


def is_admin(uid, s):
    return uid in s.admin_ids


async def referral_link(bot, tg_id: int) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=ref{tg_id}"


@router.message(Command("invite", "ref"))
async def invite(message: Message, db: Database) -> None:
    link = await referral_link(message.bot, message.from_user.id)
    invited, paid, discounts = await db.referral_stats(message.from_user.id)
    active = [d for d in discounts if d["used_at"] is None]
    lines = [
        "🤝 <b>Приведи друга — получи скидку</b>",
        "Когда друг по твоей ссылке оформит запись и оплатит её, тебе падает "
        "<b>−50%</b> на следующую заявку.",
        "",
        f"Твоя ссылка:\n{link}",
        "",
        f"Приглашено: <b>{invited}</b> · из них оплатили: <b>{paid}</b>",
    ]
    if active:
        lines.append("Доступные скидки: " + ", ".join(f"{d['percent']}%" for d in active) + " (применится в заявке)")
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Поделиться ссылкой", switch_inline_query=f"Запишись на шенген с Кипра через бота: {link}")
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.message(Command("wheel"))
async def wheel(message: Message, db: Database) -> None:
    if not await db.can_spin_wheel(message.from_user.id):
        d = await db.best_discount(message.from_user.id)
        cur = f" Твоя скидка {d['percent']}% ждёт применения в заявке." if d else ""
        await message.answer("🎡 Колесо фортуны ты уже крутил — один раз на человека." + cur)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🎡 Крутить колесо!", callback_data="wheel:spin")
    await message.answer("🎡 <b>Колесо фортуны</b>\nКрути и получи скидку на сервис: 5, 10, 15, 20 или 30%, "
                         "а с редким шансом — 100%, запись бесплатно.\n"
                         "Одна попытка на человека. Скидка действует на оплату сервиса (не на сбор визового центра) "
                         "и применится к твоей ближайшей заявке автоматически.",
                         reply_markup=kb.as_markup())


@router.callback_query(F.data == "wheel:spin")
async def wheel_spin(call: CallbackQuery, db: Database, settings: Settings) -> None:
    # Atomic claim: only the first of two fast taps wins the one allowed spin.
    if not await db.mark_wheel_spun(call.from_user.id):
        await call.answer("Ты уже крутил колесо", show_alert=True)
        return
    await call.answer()
    # Award the discount right away (before the cosmetic animation) so a claimed spin always pays out.
    percent = random.choices([p for p, _ in WHEEL], weights=[w for _, w in WHEEL])[0]
    await db.add_discount(call.from_user.id, percent, "wheel")
    await db.log_event(call.from_user.id, "wheel", str(percent))
    # little animation
    msg = call.message
    for frame in ("🎡 Крутим… 5% · 10% · 15%", "🎡 Крутим… 20% · 30% · 5%", "🎡 Почти…"):
        try:
            await msg.edit_text(frame)
            await asyncio.sleep(0.6)
        except Exception:
            break
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Записаться со скидкой", callback_data="go:order")
    try:
        won = (f"🎉 ДЖЕКПОТ! Выпало <b>100%</b> — сервис бесплатно! 🎁"
               if percent == 100 else f"🎉 Выпало <b>{percent}%</b> скидки на сервис!")
        await msg.edit_text(won + "\nСкидка уже на твоём аккаунте и применится к ближайшей заявке.",
                            reply_markup=kb.as_markup())
    except Exception:
        await msg.answer(f"🎉 Выпало {percent}% скидки!")


@router.message(Command("givediscount"))
async def give_discount(message: Message, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].lstrip("+").isdigit():
        await message.answer("Формат: <code>/givediscount @username 100</code> или <code>/givediscount tg_id 100</code>")
        return
    target, pct = parts[1], max(1, min(100, int(parts[2])))
    if target.isdigit():
        uid = int(target)
    else:
        uid = await db.tg_id_by_username(target)
        if not uid:
            await message.answer(f"Не нашёл {html.escape(target)} среди тех, кто писал боту. "
                                 "Пусть сначала нажмёт /start, или дай числовой ID.")
            return
    await db.add_discount(uid, pct, "manual")
    await message.answer(f"Скидка {pct}% выдана {html.escape(target)}.")
    try:
        await message.bot.send_message(uid, f"🎁 Тебе начислена скидка <b>{pct}%</b> на сервис — применится в заявке.")
    except Exception:
        pass

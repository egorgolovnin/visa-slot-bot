"""Orders: a client asks for a booking, the admin books it, the client pays in crypto afterwards."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import payments, security
from .config import Settings
from .db import Database
from .poller import fmt_date

log = logging.getLogger("orders")
router = Router()

STATUS = {
    "new": "🆕 новая", "in_progress": "🔎 ищем слот", "booked": "📅 забронировано, ждёт оплаты",
    "paid": "💰 оплачено", "done": "✅ завершено", "cancelled": "✖️ отменена",
}
OPEN = ("new", "in_progress", "booked", "paid")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
CONSENT = ("Оплата — после того как слот забронирован. Запись делается на твои данные, "
           "перенести её на другие даты или на другого человека нельзя. Передумал — напиши до брони.")


class NewOrder(StatesGroup):
    window = State()
    full_name = State()
    birth = State()
    nationality = State()
    passport = State()
    passport_exp = State()
    phone = State()
    email = State()
    confirm = State()


class Booking(StatesGroup):
    details = State()


def is_admin(uid: int, settings: Settings) -> bool:
    return uid in settings.admin_ids


def parse_d(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d.%m.%Y").date() if DATE_RE.match(text.strip()) else None
    except ValueError:
        return None


# ---------------- client: new order ----------------
@router.message(Command("order"))
async def order_start(message: Message, state: FSMContext, db: Database) -> None:
    if not security.enabled():
        await message.answer("Заявки временно недоступны.")
        return
    missions = await db.offered_missions()
    kb = InlineKeyboardBuilder()
    for code, name in missions:
        kb.button(text=name, callback_data=f"ord:m:{code}")
    kb.button(text="Отмена", callback_data="ord:cancel")
    kb.adjust(1)
    await state.clear()
    await message.answer("<b>Заявка на запись</b>\nВыбери страну подачи:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ord:m:"))
async def order_mission(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    code = call.data.split(":")[2]
    name = await db.mission_name(code) or code
    await state.update_data(mission=code, mission_name=name)
    await state.set_state(NewOrder.window)
    await call.message.edit_text(f"Страна: <b>{html.escape(name)}</b>")
    await call.message.answer("В какие даты ты можешь подать документы (быть на Кипре)?\n"
                              "Формат: <code>01.11.2026-30.11.2026</code> или напиши <code>любые</code>.\n/cancel — отменить")
    await call.answer()


@router.callback_query(F.data == "ord:cancel")
async def order_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Заявка отменена.")
    await call.answer()


@router.message(Command("cancel"), StateFilter("*"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(NewOrder.window, F.text)
async def order_window(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text in ("любые", "любая", "any"):
        await state.update_data(date_from=None, date_to=None)
    else:
        parts = [p.strip() for p in text.split("-")]
        d1, d2 = (parse_d(parts[0]), parse_d(parts[1])) if len(parts) == 2 else (None, None)
        if not d1 or not d2 or d2 < d1 or d2 < date.today():
            await message.answer("Не понял даты. Пример: <code>01.11.2026-30.11.2026</code> или <code>любые</code>")
            return
        await state.update_data(date_from=d1.isoformat(), date_to=d2.isoformat())
    await state.set_state(NewOrder.full_name)
    await message.answer("Имя и фамилия <b>латиницей, как в загранпаспорте</b>:\nНапример: <code>IVAN IVANOV</code>")


@router.message(NewOrder.full_name, F.text)
async def order_name(message: Message, state: FSMContext) -> None:
    name = re.sub(r"\s+", " ", message.text.strip().upper())
    if not re.fullmatch(r"[A-Z][A-Z \-']{2,80}", name) or " " not in name:
        await message.answer("Нужны имя и фамилия латиницей, как в паспорте. Пример: <code>IVAN IVANOV</code>")
        return
    await state.update_data(full_name=name)
    await state.set_state(NewOrder.birth)
    await message.answer("Дата рождения: <code>ДД.ММ.ГГГГ</code>")


@router.message(NewOrder.birth, F.text)
async def order_birth(message: Message, state: FSMContext) -> None:
    d = parse_d(message.text)
    if not d or not (date(1900, 1, 1) < d < date.today()):
        await message.answer("Формат: <code>ДД.ММ.ГГГГ</code>")
        return
    await state.update_data(birth=d.isoformat())
    await state.set_state(NewOrder.nationality)
    await message.answer("Гражданство (как в паспорте), например: <code>Russia</code>")


@router.message(NewOrder.nationality, F.text)
async def order_nat(message: Message, state: FSMContext) -> None:
    await state.update_data(nationality=message.text.strip()[:40])
    await state.set_state(NewOrder.passport)
    await message.answer("Номер загранпаспорта:")


@router.message(NewOrder.passport, F.text)
async def order_passport(message: Message, state: FSMContext) -> None:
    num = re.sub(r"\s+", "", message.text.strip().upper())
    if not re.fullmatch(r"[A-Z0-9]{5,15}", num):
        await message.answer("Номер паспорта: только буквы и цифры, без пробелов.")
        return
    await state.update_data(passport=num)
    await state.set_state(NewOrder.passport_exp)
    await message.answer("Срок действия паспорта: <code>ДД.ММ.ГГГГ</code>")


@router.message(NewOrder.passport_exp, F.text)
async def order_pexp(message: Message, state: FSMContext) -> None:
    d = parse_d(message.text)
    if not d:
        await message.answer("Формат: <code>ДД.ММ.ГГГГ</code>")
        return
    if d < date.today() + timedelta(days=90):
        await message.answer("⚠️ Паспорт должен действовать минимум 3 месяца после поездки. Проверь дату и пришли ещё раз.")
        return
    await state.update_data(passport_exp=d.isoformat())
    await state.set_state(NewOrder.phone)
    await message.answer("Телефон для связи, например <code>+35799123456</code>:")


@router.message(NewOrder.phone, F.text)
async def order_phone(message: Message, state: FSMContext) -> None:
    phone = re.sub(r"[^\d+]", "", message.text)
    if not re.fullmatch(r"\+?\d{8,15}", phone):
        await message.answer("Не похоже на номер. Пример: <code>+35799123456</code>")
        return
    await state.update_data(phone=phone)
    await state.set_state(NewOrder.email)
    await message.answer("Email (на него придёт подтверждение записи):")


@router.message(NewOrder.email, F.text)
async def order_email(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    email = message.text.strip()
    if not EMAIL_RE.match(email):
        await message.answer("Не похоже на email.")
        return
    await state.update_data(email=email)
    data = await state.get_data()
    price = await db.price(data["mission"], settings.default_price)
    window = (f"{fmt_date(date.fromisoformat(data['date_from']))}–{fmt_date(date.fromisoformat(data['date_to']))}"
              if data.get("date_from") else "любые")
    text = (
        "<b>Проверь заявку</b>\n"
        f"Страна: {html.escape(data['mission_name'])}\n"
        f"Даты подачи: {window}\n"
        f"Имя: {html.escape(data['full_name'])}\n"
        f"Дата рождения: {fmt_date(date.fromisoformat(data['birth']))}\n"
        f"Гражданство: {html.escape(data['nationality'])}\n"
        f"Паспорт: {security.mask(data['passport'])} до {fmt_date(date.fromisoformat(data['passport_exp']))}\n"
        f"Телефон: {html.escape(data['phone'])}\nEmail: {html.escape(email)}\n\n"
        f"Стоимость сервиса: <b>{price:.0f} €</b> (в крипте, после брони)\n\n{CONSENT}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить заявку", callback_data="ord:confirm")
    kb.button(text="Отмена", callback_data="ord:cancel")
    kb.adjust(1)
    await state.set_state(NewOrder.confirm)
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "ord:confirm", NewOrder.confirm)
async def order_confirm(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    await state.clear()
    applicant = {k: data[k] for k in ("full_name", "birth", "nationality", "passport", "passport_exp", "phone", "email")}
    d_from = date.fromisoformat(data["date_from"]) if data.get("date_from") else None
    d_to = date.fromisoformat(data["date_to"]) if data.get("date_to") else None
    order_id = await db.create_order(call.from_user.id, data["mission"], d_from, d_to, security.encrypt(applicant))
    subs = {r["mission"] for r in await db.user_subs(call.from_user.id)}
    if data["mission"] not in subs:
        await db.toggle_sub(call.from_user.id, data["mission"])
    await db.log_event(call.from_user.id, "order_new", f"#{order_id} {data['mission']}")
    await call.message.edit_text(
        f"✅ Заявка <b>#{order_id}</b> принята.\nИщем слот: {html.escape(data['mission_name'])}. "
        "Напишу, как только запись будет сделана. Статус — /myorders")
    await call.answer()
    for admin_id in settings.admin_ids:
        await send_admin_card(call.bot, db, admin_id, order_id, settings, header="🆕 Новая заявка")


# ---------------- client: my orders ----------------
@router.message(Command("myorders"))
async def my_orders(message: Message, db: Database) -> None:
    rows = await db.orders_list(tuple(STATUS), tg_id=message.from_user.id, limit=10)
    if not rows:
        await message.answer("Заявок нет. Оформить — /order")
        return
    lines, kb = ["<b>Мои заявки</b>"], InlineKeyboardBuilder()
    for r in rows:
        lines.append(f"#{r['id']} {html.escape(r['mission_name'] or r['mission'])} — {STATUS.get(r['status'], r['status'])}"
                     + (f"\n     запись: {html.escape(r['appointment'])}" if r["appointment"] else ""))
        if r["status"] == "booked" and r["invoice_url"]:
            kb.button(text=f"Оплатить #{r['id']} — {r['price_eur']:.0f} €", url=r["invoice_url"])
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


# ---------------- admin ----------------
def admin_buttons(o) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    oid, st = o["id"], o["status"]
    if st == "new":
        kb.button(text="🔎 Взять в работу", callback_data=f"oa:take:{oid}")
    if st in ("new", "in_progress"):
        kb.button(text="📅 Забронировано", callback_data=f"oa:book:{oid}")
    if st == "booked":
        kb.button(text="💰 Оплачено вручную", callback_data=f"oa:paid:{oid}")
        kb.button(text="🔁 Новый счёт", callback_data=f"oa:invoice:{oid}")
    if st == "paid":
        kb.button(text="✅ Завершить", callback_data=f"oa:done:{oid}")
    if st in ("new", "in_progress", "booked"):
        kb.button(text="✖️ Отменить", callback_data=f"oa:cancel:{oid}")
    kb.adjust(2)
    return kb


def admin_card_text(o, tz: ZoneInfo) -> str:
    a = security.decrypt(o["applicant"])
    window = f"{fmt_date(o['date_from'])}–{fmt_date(o['date_to'])}" if o["date_from"] else "любые"
    lines = [
        f"<b>Заявка #{o['id']}</b> · {STATUS.get(o['status'], o['status'])}",
        f"Страна: <b>{html.escape(o['mission_name'] or o['mission'])}</b> · даты: {window}",
        f"Клиент: {o['user_name'] or '—'}" + (f" @{html.escape(o['username'])}" if o["username"] else "")
        + f" (<code>{o['tg_id']}</code>)",
        f"Создана: {o['created_at'].astimezone(tz).strftime('%d.%m %H:%M')}",
        "",
        f"<code>{html.escape(a.get('full_name', ''))}</code>",
        f"Рождение: {a.get('birth', '')} · {html.escape(a.get('nationality', ''))}",
        f"Паспорт: <code>{html.escape(a.get('passport', ''))}</code> до {a.get('passport_exp', '')}",
        f"Тел: <code>{html.escape(a.get('phone', ''))}</code> · <code>{html.escape(a.get('email', ''))}</code>",
    ]
    if o["appointment"]:
        lines.append(f"\nЗапись: <b>{html.escape(o['appointment'])}</b>")
    if o["price_eur"] is not None:
        lines.append(f"Цена: {o['price_eur']:.0f} €" + (f" · оплачено {o['paid_amount']} {o['paid_asset']}" if o["paid_asset"] else ""))
    return "\n".join(lines)


async def send_admin_card(bot: Bot, db: Database, chat_id: int, order_id: int, settings: Settings,
                          header: str = "") -> None:
    o = await db.order(order_id)
    if not o:
        return
    text = (header + "\n\n" if header else "") + admin_card_text(o, ZoneInfo(settings.tz))
    try:
        await bot.send_message(chat_id, text, reply_markup=admin_buttons(o).as_markup())
    except Exception:
        log.exception("admin card to %s failed", chat_id)


@router.message(Command("orders"))
async def orders_cmd(message: Message, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    rows = await db.orders_list(OPEN, limit=40)
    if not rows:
        await message.answer("Открытых заявок нет.")
        return
    lines, kb = ["<b>Открытые заявки</b>"], InlineKeyboardBuilder()
    for r in rows:
        window = f"{fmt_date(r['date_from'])}–{fmt_date(r['date_to'])}" if r["date_from"] else "любые"
        lines.append(f"#{r['id']} {html.escape(r['mission_name'] or r['mission'])} · {window} · {STATUS[r['status']]}")
        kb.button(text=f"#{r['id']}", callback_data=f"oa:open:{r['id']}")
    kb.adjust(5)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.message(Command("order_card"))
async def order_card_cmd(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if is_admin(message.from_user.id, settings) and (command.args or "").strip().isdigit():
        await send_admin_card(message.bot, db, message.chat.id, int(command.args), settings)


@router.callback_query(F.data.startswith("oa:"))
async def order_admin(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not is_admin(call.from_user.id, settings):
        await call.answer("Только для админа", show_alert=True)
        return
    _, action, oid = call.data.split(":")
    oid = int(oid)
    o = await db.order(oid)
    if not o:
        await call.answer("Нет такой заявки", show_alert=True)
        return
    if action == "open":
        await send_admin_card(call.bot, db, call.message.chat.id, oid, settings)
    elif action == "take":
        await db.update_order(oid, status="in_progress")
        await notify_client(call.bot, o["tg_id"], f"🔎 Заявка #{oid}: начали искать слот.")
    elif action == "book":
        await state.set_state(Booking.details)
        await state.update_data(order_id=oid)
        price = o["price_eur"] if o["price_eur"] is not None else await db.price(o["mission"], settings.default_price)
        await call.message.answer(
            f"Заявка #{oid}. Пришли <b>одним сообщением</b>:\n"
            "• PDF подтверждения записи файлом, а в подписи — дату, время и место\n"
            "  или просто текст, если PDF нет.\n"
            f"Пример подписи: <code>28.10.2026 10:00, VFS Nicosia</code>\n"
            f"Цена по умолчанию {price:.0f} €. Другая цена — допиши в конце: <code>… ; 120</code>\n/cancel — отмена")
    elif action == "paid":
        await mark_paid(call.bot, db, settings, oid, asset="manual", amount=str(o["price_eur"] or ""))
    elif action == "invoice":
        await issue_invoice(call.bot, db, settings, oid)
    elif action == "done":
        await db.update_order(oid, status="done")
    elif action == "cancel":
        await db.update_order(oid, status="cancelled")
        await notify_client(call.bot, o["tg_id"], f"✖️ Заявка #{oid} отменена. Вопросы — напиши админу.")
    if action in ("take", "done", "cancel", "paid"):
        o = await db.order(oid)
        try:
            await call.message.edit_text(admin_card_text(o, ZoneInfo(settings.tz)),
                                         reply_markup=admin_buttons(o).as_markup())
        except Exception:
            pass
    await call.answer()


@router.message(Booking.details, F.document | F.text)
async def booking_details(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    data = await state.get_data()
    oid = data["order_id"]
    raw = (message.caption if message.document else message.text) or ""
    parts = [p.strip() for p in raw.split(";")]
    appointment, price = parts[0], None
    if len(parts) > 1 and re.fullmatch(r"\d+(\.\d+)?", parts[-1]):
        price = float(parts[-1])
    if not appointment:
        await message.answer("Нужна дата и время записи в тексте или подписи к PDF.")
        return
    o = await db.order(oid)
    if price is None:
        price = float(o["price_eur"]) if o["price_eur"] is not None else await db.price(o["mission"], settings.default_price)
    await db.update_order(oid, status="booked", appointment=appointment[:200], price_eur=price,
                          confirmation_file=message.document.file_id if message.document else None)
    await state.clear()
    await db.log_event(o["tg_id"], "order_booked", f"#{oid}")
    await issue_invoice(message.bot, db, settings, oid)
    await message.answer(f"Готово: заявка #{oid} забронирована, клиенту отправлен счёт на {price:.0f} €.")


# ---------------- money ----------------
async def notify_client(bot: Bot, uid: int, text: str, **kw) -> None:
    try:
        await bot.send_message(uid, text, **kw)
    except Exception:
        log.warning("cannot message client %s", uid)


async def issue_invoice(bot: Bot, db: Database, settings: Settings, oid: int) -> None:
    o = await db.order(oid)
    name = o["mission_name"] or o["mission"]
    head = (f"✅ <b>Слот забронирован!</b>\nЗаявка #{oid}: {html.escape(name)}\n"
            f"Запись: <b>{html.escape(o['appointment'] or '')}</b>\n\n")
    kb = InlineKeyboardBuilder()
    if payments.enabled():
        try:
            invoice_id, url = await payments.create_invoice(oid, float(o["price_eur"]), f"Visa slot service, order #{oid}, {name}")
            await db.update_order(oid, invoice_id=invoice_id, invoice_url=url)
            kb.button(text=f"💳 Оплатить {o['price_eur']:.0f} € криптой", url=url)
            pay = "Оплати сервис по кнопке ниже (USDT, TON, BTC и др.). Как только оплата пройдёт, бот это увидит."
        except payments.PaymentError as e:
            log.error("invoice failed: %s", e)
            pay = f"Стоимость: {o['price_eur']:.0f} €. Реквизиты для оплаты пришлёт админ."
            for a in settings.admin_ids:
                await notify_client(bot, a, f"⚠️ Не удалось создать счёт для #{oid}: <code>{html.escape(str(e))}</code>")
    else:
        pay = f"Стоимость: {o['price_eur']:.0f} €. Реквизиты для оплаты пришлёт админ."
    pdf_note = "\n\nPDF подтверждения пришлю сразу после оплаты." if (o["confirmation_file"] and settings.pdf_after_payment) else ""
    await notify_client(bot, o["tg_id"], head + pay + pdf_note, reply_markup=kb.as_markup() if payments.enabled() else None)
    if o["confirmation_file"] and not settings.pdf_after_payment:
        try:
            await bot.send_document(o["tg_id"], o["confirmation_file"], caption=f"Подтверждение записи, заявка #{oid}")
        except Exception:
            log.warning("cannot send pdf for %s", oid)


async def mark_paid(bot: Bot, db: Database, settings: Settings, oid: int, asset: str, amount: str) -> None:
    o = await db.order(oid)
    if not o or o["status"] in ("paid", "done"):
        return
    await db.update_order(oid, status="paid", paid_at=datetime.now(timezone.utc), paid_asset=asset, paid_amount=amount)
    await db.log_event(o["tg_id"], "order_paid", f"#{oid} {amount} {asset}")
    await notify_client(bot, o["tg_id"], f"💰 Оплата по заявке #{oid} получена, спасибо!\n"
                                         f"Запись: <b>{html.escape(o['appointment'] or '')}</b>. Удачи на подаче!")
    if o["confirmation_file"] and settings.pdf_after_payment:
        try:
            await bot.send_document(o["tg_id"], o["confirmation_file"], caption=f"Подтверждение записи, заявка #{oid}")
        except Exception:
            log.warning("cannot send pdf for %s", oid)
    for a in settings.admin_ids:
        await notify_client(bot, a, f"💰 Заявка #{oid} оплачена: {amount} {asset}")


async def payment_watcher(bot: Bot, db: Database, settings: Settings, every: int = 60) -> None:
    while True:
        await asyncio.sleep(every)
        if not payments.enabled():
            continue
        try:
            rows = await db.awaiting_payment()
            paid = await payments.paid_invoices([r["invoice_id"] for r in rows])
            for r in rows:
                inv = paid.get(int(r["invoice_id"]))
                if inv:
                    await mark_paid(bot, db, settings, r["id"], inv.get("paid_asset") or inv.get("asset") or "crypto",
                                    str(inv.get("paid_amount") or inv.get("amount") or ""))
        except Exception:
            log.exception("payment watcher")


# ---------------- prices ----------------
@router.message(Command("price"))
async def price_cmd(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    args = (command.args or "").split()
    if len(args) == 2 and re.fullmatch(r"\d+(\.\d+)?", args[1]):
        await db.set_price(args[0].lower(), float(args[1]))
        await message.answer(f"Цена {args[0].lower()}: {float(args[1]):.0f} €")
        return
    lines = [f"<b>Цены</b> (по умолчанию {settings.default_price:.0f} €)"]
    for r in await db.prices():
        lines.append(f"<code>{r['code']}</code> {html.escape(r['name'])}: "
                     + (f"{r['price_eur']:.0f} €" if r["price_eur"] is not None else "по умолчанию"))
    lines.append("\nИзменить: /price hun 120")
    payment_state = "подключена" if payments.enabled() else "не подключена (нужен CRYPTO_PAY_TOKEN)"
    lines.append(f"Оплата Crypto Pay: {payment_state}")
    await message.answer("\n".join(lines))

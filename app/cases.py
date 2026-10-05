"""Visa outcome cases: clients (and anyone) share how their application went; admins moderate;
published cases feed the public statistics page /cases."""
from __future__ import annotations

import html
import logging
from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .db import Database
from .parsing import parse_date

log = logging.getLogger("cases")
router = Router()
NOT_A_COMMAND = ~F.text.startswith("/")
ASK_AFTER_DAYS = 21  # after the appointment

# countries people apply for from Cyprus (any Schengen state, not only the ones we book)
COUNTRIES = ["Австрия", "Бельгия", "Венгрия", "Германия", "Греция", "Дания", "Испания", "Италия", "Латвия", "Литва",
             "Люксембург", "Мальта", "Нидерланды", "Норвегия", "Польша", "Португалия", "Словакия", "Словения",
             "Финляндия", "Франция", "Хорватия", "Чехия", "Швейцария", "Швеция", "Эстония", "Исландия"]
OUTCOMES = {"approved": "✅ Одобрили", "refused": "❌ Отказ", "pending": "⏳ Ещё жду"}


class Case(StatesGroup):
    country = State()
    submitted = State()
    outcome = State()
    decided = State()
    kind = State()
    comment = State()
    consent = State()


def _kb(*rows: list[tuple[str, str]]):
    kb = InlineKeyboardBuilder()
    sizes = []
    for row in rows:
        for text, data in row:
            kb.button(text=text, callback_data=data)
        sizes.append(len(row))
    kb.adjust(*sizes)
    return kb.as_markup()


async def start_case(message: Message, state: FSMContext, country: str | None = None,
                     submitted: date | None = None, order_id: int | None = None) -> None:
    await state.clear()
    await state.update_data(order_id=order_id)
    await message.answer("📊 <b>Поделись результатом подачи</b>\n"
                         "Мы собираем открытую статистику виз с Кипра: какие страны одобряют, сколько ждать. "
                         "Кейс публикуется анонимно — без имени и контактов. Займёт минуту.")
    if country:
        await state.update_data(country=country)
        if submitted:
            await state.update_data(submitted=submitted.isoformat())
            await ask_outcome(message, state)
        else:
            await ask_submitted(message, state)
        return
    await state.set_state(Case.country)
    kb = InlineKeyboardBuilder()
    for c in COUNTRIES:
        kb.button(text=c, callback_data=f"case:c:{c}")
    kb.button(text="✖️ Отмена", callback_data="case:cancel")
    kb.adjust(3)
    await message.answer("В какую страну подавал?", reply_markup=kb.as_markup())


@router.message(Command("case"))
async def case_cmd(message: Message, state: FSMContext) -> None:
    await start_case(message, state)


@router.callback_query(F.data == "case:start")
async def case_start_cb(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await start_case(call.message, state)


@router.callback_query(F.data.startswith("case:order:"))
async def case_from_order(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    from .orders import DESTINATIONS
    oid = int(call.data.split(":")[2])
    o = await db.order(oid)
    await call.answer()
    if not o or o["tg_id"] != call.from_user.id:
        return
    country = DESTINATIONS.get(o["destination"], (None,))[0]
    await start_case(call.message, state, country=country, submitted=parse_date(o["appointment"] or ""), order_id=oid)


@router.callback_query(F.data == "case:cancel")
async def case_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Ок, в другой раз. Поделиться можно в любой момент — /case")
    await call.answer()


@router.callback_query(F.data.startswith("case:c:"), Case.country)
async def case_country(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(country=call.data.split(":", 2)[2])
    await call.answer()
    await ask_submitted(call.message, state)


async def ask_submitted(message: Message, state: FSMContext) -> None:
    await state.set_state(Case.submitted)
    await message.answer("Когда подавал документы? Например <code>12.09.2026</code> или <code>сентябрь 2026</code>.",
                         reply_markup=_kb([("Не помню", "case:skipdate")], [("✖️ Отмена", "case:cancel")]))


@router.message(Case.submitted, F.text, NOT_A_COMMAND)
async def case_submitted(message: Message, state: FSMContext) -> None:
    from .parsing import parse_human_window
    d = parse_date(message.text)
    if not d:
        w = parse_human_window(message.text, today=date.today() - timedelta(days=365))
        d = w[0] if w else None
    if not d or d > date.today():
        await message.answer("Не понял дату. Пример: <code>12.09.2026</code>", reply_markup=_kb([("Не помню", "case:skipdate")]))
        return
    await state.update_data(submitted=d.isoformat())
    await ask_outcome(message, state)


@router.callback_query(F.data == "case:skipdate", Case.submitted)
async def case_skip_submitted(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(submitted=None)
    await call.answer()
    await ask_outcome(call.message, state)


async def ask_outcome(message: Message, state: FSMContext) -> None:
    await state.set_state(Case.outcome)
    await message.answer("Какой результат?", reply_markup=_kb([(v, f"case:o:{k}") for k, v in OUTCOMES.items()],
                                                              [("✖️ Отмена", "case:cancel")]))


@router.callback_query(F.data.startswith("case:o:"), Case.outcome)
async def case_outcome(call: CallbackQuery, state: FSMContext) -> None:
    outcome = call.data.split(":")[2]
    await state.update_data(outcome=outcome)
    await call.answer()
    if outcome == "pending":
        await ask_comment(call.message, state)
        return
    await state.set_state(Case.decided)
    await call.message.answer("Когда получил паспорт с решением? Например <code>01.10.2026</code>.",
                              reply_markup=_kb([("Не помню", "case:skipdecided")]))


@router.message(Case.decided, F.text, NOT_A_COMMAND)
async def case_decided(message: Message, state: FSMContext) -> None:
    d = parse_date(message.text)
    data = await state.get_data()
    sub = date.fromisoformat(data["submitted"]) if data.get("submitted") else None
    if not d or d > date.today() or (sub and d < sub):
        await message.answer("Не понял дату (она должна быть после подачи). Пример: <code>01.10.2026</code>",
                             reply_markup=_kb([("Не помню", "case:skipdecided")]))
        return
    await state.update_data(decided=d.isoformat())
    await after_decided(message, state)


@router.callback_query(F.data == "case:skipdecided", Case.decided)
async def case_skip_decided(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(decided=None)
    await call.answer()
    await after_decided(call.message, state)


async def after_decided(message: Message, state: FSMContext) -> None:
    if (await state.get_data()).get("outcome") == "approved":
        await state.set_state(Case.kind)
        await message.answer("Какую визу дали?", reply_markup=_kb(
            [("Однократная", "case:k:single"), ("Многократная", "case:k:multi")], [("Пропустить", "case:k:")]))
    else:
        await ask_comment(message, state)


@router.callback_query(F.data.startswith("case:k:"), Case.kind)
async def case_kind(call: CallbackQuery, state: FSMContext) -> None:
    kind = call.data.split(":")[2] or None
    await state.update_data(visa_kind=kind)
    await call.answer()
    await ask_comment(call.message, state, hint=" Например, на какой срок визу дали." if kind else "")


async def ask_comment(message: Message, state: FSMContext, hint: str = "") -> None:
    await state.set_state(Case.comment)
    await message.answer("Пара слов для других: что спрашивали, каких документов не хватило, советы." + hint
                         + "\nБез имён и контактов.", reply_markup=_kb([("Пропустить", "case:nocomment")]))


@router.message(Case.comment, F.text, NOT_A_COMMAND)
async def case_comment(message: Message, state: FSMContext) -> None:
    await state.update_data(comment=message.text.strip()[:600])
    await ask_consent(message, state)


@router.callback_query(F.data == "case:nocomment", Case.comment)
async def case_no_comment(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(comment=None)
    await call.answer()
    await ask_consent(call.message, state)


async def ask_consent(message: Message, state: FSMContext) -> None:
    await state.set_state(Case.consent)
    await message.answer("Можно опубликовать этот кейс анонимно на странице статистики?",
                         reply_markup=_kb([("Да, публикуйте", "case:pub:1")], [("Только для статистики", "case:pub:0")]))


@router.callback_query(F.data.startswith("case:pub:"), Case.consent)
async def case_finish(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    await state.clear()
    publish = call.data.endswith("1")
    case_id = await db.add_case(
        tg_id=call.from_user.id, order_id=data.get("order_id"), country=data["country"],
        submitted_on=date.fromisoformat(data["submitted"]) if data.get("submitted") else None,
        decided_on=date.fromisoformat(data["decided"]) if data.get("decided") else None,
        outcome=data["outcome"], visa_kind=data.get("visa_kind"), comment=data.get("comment"), publish=publish)
    await db.log_event(call.from_user.id, "case", f"{data['country']} {data['outcome']}")
    await call.answer()
    await call.message.answer("Спасибо! Кейс поможет другим понять шансы и сроки 🙌")
    for a in settings.admin_ids:
        try:
            await call.bot.send_message(a, admin_case_text(await db.case(case_id)), reply_markup=_kb(
                [("✅ Опубликовать", f"casea:pub:{case_id}"), ("🙈 Скрыть", f"casea:hide:{case_id}")]))
        except Exception:
            log.exception("case to admin")


def admin_case_text(c) -> str:
    days = (c["decided_on"] - c["submitted_on"]).days if c["decided_on"] and c["submitted_on"] else None
    return (f"📊 <b>Новый кейс #{c['id']}</b>\n{html.escape(c['country'])} · {OUTCOMES[c['outcome']]}"
            + (f"\nПодача: {c['submitted_on']:%d.%m.%Y}" if c["submitted_on"] else "")
            + (f"\nРешение: {c['decided_on']:%d.%m.%Y} ({days} дн)" if c["decided_on"] else "")
            + (f"\nВиза: {'многократная' if c['visa_kind'] == 'multi' else 'однократная'}" if c["visa_kind"] else "")
            + (f"\n«{html.escape(c['comment'])}»" if c["comment"] else "")
            + ("\nКлиент разрешил публикацию" if c["publish"] else "\nТолько для статистики, без текста"))


@router.callback_query(F.data.startswith("casea:"))
async def case_admin(call: CallbackQuery, db: Database, settings: Settings) -> None:
    if call.from_user.id not in settings.admin_ids:
        await call.answer("Только для админа", show_alert=True)
        return
    _, action, cid = call.data.split(":")
    await db.set_case_status(int(cid), "published" if action == "pub" else "hidden")
    await call.answer("Опубликовано" if action == "pub" else "Скрыто")
    try:
        await call.message.edit_text(call.message.html_text + ("\n\n✅ Опубликован" if action == "pub" else "\n\n🙈 Скрыт"))
    except Exception:
        pass


async def ask_for_cases(bot: Bot, db: Database) -> None:
    """Three weeks after the appointment, ask the client how it went."""
    from .orders import DESTINATIONS
    for o in await db.orders_to_ask_case():
        d = parse_date(o["appointment"] or "")
        if not d or date.today() < d + timedelta(days=ASK_AFTER_DAYS):
            continue
        await db.update_order(o["id"], case_asked=True)
        country = DESTINATIONS.get(o["destination"], (o["mission"],))[0]
        try:
            await bot.send_message(o["tg_id"], f"Привет! Как прошла подача в {html.escape(country)} ({d:%d.%m})? "
                                               "Если визу уже дали — поделишься результатом? Это займёт минуту и "
                                               "поможет другим 🙏", reply_markup=_kb([("Поделиться", f"case:order:{o['id']}")]))
        except Exception:
            log.warning("cannot ask case for order %s", o["id"])

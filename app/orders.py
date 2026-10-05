"""Orders: a client asks for a booking, the admin books it, the client pays in crypto afterwards."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta, timezone  # noqa: F401
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
    "awaiting_fee": "💳 ждёт оплаты сбора",
    "new": "🆕 новая", "in_progress": "🔎 ищем слот", "booked": "📅 забронировано, ждёт оплаты",
    "paid": "💰 оплачено", "done": "✅ завершено", "cancelled": "✖️ отменена",
}
OPEN = ("awaiting_fee", "new", "in_progress", "booked", "paid")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
FEE_NOTE = ("\n\nСбор визового центра мы платим при бронировании от твоего имени. Если до брони передумаешь "
            "или мы не сможем найти слот — вернём его.")
CONSENT = ("📌 Отправляя заявку, ты соглашаешься с условиями: услуга оказана, когда мы прислали подтверждение записи, "
           "после этого её нужно оплатить. Передумал — напиши в /support до брони. "
           "Слот может появиться всего за 2–7 дней до подачи — собери документы заранее. Полные условия — /terms")


class NewOrder(StatesGroup):
    window = State()
    count = State()
    same_day = State()
    full_name = State()
    gender = State()
    birth = State()
    nationality = State()
    passport = State()
    passport_exp = State()
    passport_scan = State()
    residence_exp = State()
    phone = State()
    email = State()
    confirm = State()


class Booking(StatesGroup):
    details = State()


# destination country -> (name, mission that handles it, note shown to the client)
DESTINATIONS: dict[str, tuple[str, str, str]] = {
    "aut": ("Австрия", "aut", "VFS Global, Никосия"),
    "hun": ("Венгрия", "hun", "VFS Global, Никосия"),
    "svk": ("Словакия", "svk", "VFS Global, Никосия"),
    "cze": ("Чехия", "svk", "подача через визовый центр Словакии"),
    "svn": ("Словения", "svk", "подача через визовый центр Словакии"),
    "fin": ("Финляндия", "fin", "VFS Global, Никосия"),
    "swe": ("Швеция", "fin", "подача через визовый центр Финляндии"),
    "hrv": ("Хорватия", "hrv", "VFS Global, Никосия"),
    "deu": ("Германия", "deu", "посольство Германии, Никосия"),
    "nld": ("Нидерланды", "deu", "подача в посольстве Германии"),
    "bel": ("Бельгия", "deu", "подача в посольстве Германии"),
    "dnk": ("Дания", "deu", "подача в посольстве Германии"),
    "nor": ("Норвегия", "deu", "подача в посольстве Германии"),
    "isl": ("Исландия", "deu", "подача в посольстве Германии"),
    "lux": ("Люксембург", "deu", "подача в посольстве Германии"),
    "grc": ("Греция", "grc", "посольство Греции, Никосия"),
    "mlt": ("Мальта", "grc", "подача в посольстве Греции"),
}

from .requirements_by_country import form_steps, requirements  # noqa: E402

STEPS = ["window", "full_name", "gender", "birth", "nationality", "passport", "passport_exp",
         "passport_scan", "residence_exp", "phone", "email"]  # superset, real order: form_steps(mission)
QUESTIONS = {
    "window": ("В какие даты ты можешь подать документы (быть на Кипре)?\n"
               "Пиши как удобно: <code>ноябрь</code>, <code>конец ноября</code>, <code>5-20 ноября</code>, "
               "<code>ближайшие 2 недели</code>, <code>01.11.2026-30.11.2026</code>. Если неважно — «Любые даты»."),
    "full_name": "Имя и фамилия <b>латиницей, как в загранпаспорте</b>.\nНапример: <code>IVAN IVANOV</code>",
    "birth": "Дата рождения, например <code>15.03.1990</code>",
    "nationality": "Гражданство — выбери кнопкой или напиши, как в паспорте (например <code>Russia</code>):",
    "passport": "Номер загранпаспорта (буквы и цифры):",
    "passport_exp": "До какого числа действует паспорт? Например <code>01.01.2031</code>",
    "gender": "Пол (как в паспорте):",
    "passport_scan": ("Пришли <b>фото разворота паспорта с фотографией</b> — его требует визовый центр при записи. "
                      "Фото должно быть чётким, все поля читаются. Файл видят только сотрудники сервиса."),
    "residence_exp": ("До какого числа действует твой <b>ВНЖ Кипра</b>? Например <code>15.08.2027</code>\n"
                      "Визу дают только если ВНЖ действует минимум 3 месяца после возвращения из поездки."),
    "phone": ("Номер телефона — его обязательно требует визовый центр в форме записи. "
              "Например <code>+35799123456</code>.\nСвязываться с тобой будем здесь, в Telegram."),
    "email": "Email — на него придёт подтверждение записи:",
}
NOT_A_COMMAND = ~F.text.startswith("/")


def is_admin(uid: int, settings: Settings) -> bool:
    return uid in settings.admin_ids


def step_kb(step: str):
    kb = InlineKeyboardBuilder()
    if step == "gender":
        kb.button(text="Мужской", callback_data="ord:g:M")
        kb.button(text="Женский", callback_data="ord:g:F")
    if step != "window":
        kb.button(text="⬅️ Назад", callback_data="ord:back")
    if step == "window":
        kb.button(text="Любые даты", callback_data="ord:any")
    if step == "residence_exp":
        kb.button(text="ВНЖ на продлении (есть ресипт)", callback_data="ord:receipt")
    kb.button(text="✖️ Отмена", callback_data="ord:cancel")
    kb.adjust(2)
    return kb.as_markup()


async def ask(message: Message, state: FSMContext, step: str, prefix: str = "") -> None:
    data = await state.get_data()
    if step == "count":
        await state.set_state(NewOrder.count)
        kb = InlineKeyboardBuilder()
        for n in range(1, MAX_PEOPLE + 1):
            kb.button(text=("Только я" if n == 1 else f"{n} человека" if n < 5 else f"{n} человек"), callback_data=f"ord:n:{n}")
        kb.button(text="⬅️ Назад", callback_data="ord:back")
        kb.adjust(1, 2, 2, 1)
        await message.answer("Сколько человек подаётся? Каждому нужен свой слот, запишем всех по одной заявке.",
                             reply_markup=kb.as_markup())
        return
    if data.get("editing") and step != "window":
        prefix = prefix or "Исправляем. "
    n = data.get("n", 1)
    if n > 1 and step in PERSONAL_KEYS + ("passport_scan",):
        prefix = f"👤 <b>Человек {data.get('pi', 0) + 1} из {n}.</b> " + prefix
    await state.set_state(getattr(NewOrder, step))
    kb = nationality_kb([]) if step == "nationality" else step_kb(step)
    await message.answer(prefix + QUESTIONS[step], reply_markup=kb)


async def next_step(message: Message, state: FSMContext, current: str, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    mission = data.get("mission", "")
    steps = personal_steps(mission)
    if current in steps:
        idx = steps.index(current)
        if data.get("editing") == "field":  # one field of one person, then back to the summary
            await commit_person(state)
            await state.update_data(editing=False)
            await show_confirm(message, state, db, settings)
            return
        if idx + 1 < len(steps):
            await ask(message, state, steps[idx + 1])
            return
        await commit_person(state)
        data = await state.get_data()
        if data.get("editing") == "person":
            await state.update_data(editing=False)
            await show_confirm(message, state, db, settings)
            return
        dup = _duplicate_passport(data.get("people") or [])
        if dup:
            await state.update_data(pi=len(data["people"]) - 1)
            await load_person(state, len(data["people"]) - 1)
            await message.answer(f"Паспорт {html.escape(dup)} уже есть у другого человека в заявке — проверь номер.")
            await ask(message, state, "passport")
            return
        if len(data["people"]) < data.get("n", 1):
            await state.update_data(pi=len(data["people"]))
            await ask(message, state, steps[0])
            return
        if "phone" in form_steps(mission) and not data.get("phone"):
            await ask(message, state, "phone")
            return
        await show_confirm(message, state, db, settings)
        return
    if data.get("editing"):  # a shared field (dates, phone) fixed from the summary
        await state.update_data(editing=False)
        await show_confirm(message, state, db, settings)
        return
    if current == "window":
        await ask(message, state, "count")
    elif current == "phone":
        await show_confirm(message, state, db, settings)


def _has_child(people: list[dict]) -> bool:
    today = date.today()
    for p in people:
        if (p or {}).get("birth"):
            b = date.fromisoformat(p["birth"])
            if (today.year - b.year - ((today.month, today.day) < (b.month, b.day))) < 12:
                return True
    return False


def _duplicate_passport(people: list[dict]) -> str | None:
    seen = set()
    for p in people:
        num = (p or {}).get("passport")
        if num and num in seen:
            return num
        seen.add(num)
    return None


PERSONAL_KEYS = ("full_name", "gender", "birth", "nationality", "passport", "passport_exp", "passport_scan",
                 "residence_exp", "residence_receipt")
MAX_PEOPLE = 5  # VFS noOfMaxAllowedApplicant


def personal_steps(mission: str) -> list[str]:
    return [s for s in form_steps(mission) if s not in ("window", "phone")]


async def commit_person(state: FSMContext) -> None:
    data = await state.get_data()
    person = {k: data.get(k) for k in PERSONAL_KEYS}
    people = list(data.get("people") or [])
    pi = data.get("pi", 0)
    if pi < len(people):
        people[pi] = person
    else:
        people.append(person)
    await state.update_data(people=people, **{k: None for k in PERSONAL_KEYS})


async def load_person(state: FSMContext, k: int) -> None:
    people = (await state.get_data()).get("people") or []
    person = people[k] if k < len(people) else {}
    await state.update_data(pi=k, **{key: person.get(key) for key in PERSONAL_KEYS})


def parse_d(text: str) -> date | None:
    from .parsing import parse_date
    return parse_date(text)


# ---------------- client: new order ----------------
async def order_block_reason(db: Database, settings: Settings, uid: int, mission: str | None = None) -> str | None:
    """Why this person cannot open an order (for this country), or None if they can."""
    if is_admin(uid, settings):
        return None
    taken = await db.open_order_missions(uid)
    if mission and mission in taken:
        return "По этой стране (или по стране с той же подачей) у тебя уже есть активная заявка. Статус — /myorders"
    if len(taken) >= settings.max_order_countries and mission not in taken:
        return (f"Можно вести заявки максимум по {settings.max_order_countries} странам одновременно. "
                "Новая станет доступна, когда одна из текущих завершится или будет отменена. Статус — /myorders")
    return None


@router.message(Command("order"))
async def order_start(message: Message, state: FSMContext, db: Database, settings: Settings,
                      uid: int | None = None) -> None:
    uid = uid or message.from_user.id
    await state.clear()
    if not security.enabled():
        await message.answer("Заявки временно недоступны.")
        return
    reason = await order_block_reason(db, settings, uid)
    if reason:
        await message.answer(reason)
        return
    offered = {c for c, _ in await db.offered_missions()}
    taken = set() if is_admin(uid, settings) else await db.open_order_missions(uid)
    kb = InlineKeyboardBuilder()
    n = 0
    for key, (name, mission, _) in sorted(DESTINATIONS.items(), key=lambda kv: kv[1][0]):
        if mission in offered and mission not in taken:
            kb.button(text=name, callback_data=f"ord:d:{key}")
            n += 1
    kb.button(text=f"📂 Собрать документы за меня — {settings.docs_price:.0f} €", callback_data="docs:req")
    kb.button(text="✖️ Отмена", callback_data="ord:cancel")
    kb.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 1, 1)  # countries in pairs, then two wide buttons
    from .documents import PROCESS
    await message.answer(PROCESS.format(price=f"{settings.default_price:.0f}", docs_price=f"{settings.docs_price:.0f}"))
    await message.answer("<b>Заявка на запись</b>\nВ какую страну едешь (основная страна поездки)?",
                         reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ord:d:"))
async def order_destination(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    key = call.data.split(":")[2]
    if key not in DESTINATIONS:
        await call.answer("Кнопка устарела, начни заново: /order", show_alert=True)
        return
    name, mission, note = DESTINATIONS[key]
    reason = await order_block_reason(db, settings, call.from_user.id, mission)
    if reason:
        await call.message.edit_text(reason)
        await call.answer()
        return
    await state.clear()
    tg = f"@{call.from_user.username}" if call.from_user.username else None
    await state.update_data(mission=mission, destination=key, mission_name=name, note=note, tg_contact=tg)
    await call.message.edit_text(f"Страна: <b>{name}</b> ({note})")
    await call.answer()
    from .documents import links_html
    docs = links_html(mission, key)
    if docs:
        dkb = InlineKeyboardBuilder()
        dkb.button(text=f"📂 Собрать документы за меня — {settings.docs_price:.0f} €", callback_data="docs:req")
        await call.message.answer(f"📄 <b>Что понадобится на подаче</b> ({name}):\n{docs}\n\n"
                                  "Документы приносишь в день записи. Ссылки ещё раз пришлём вместе с подтверждением брони.",
                                  reply_markup=dkb.as_markup(), disable_web_page_preview=True)
    await ask(call.message, state, "window")


@router.callback_query(F.data == "ord:cancel")
async def order_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.edit_text("Заявка отменена. Начать заново — /order")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "ord:back")
async def order_back(call: CallbackQuery, state: FSMContext) -> None:
    current = (await state.get_state() or "").split(":")[-1]
    data = await state.get_data()
    steps = personal_steps(data.get("mission", ""))
    await call.answer()
    if current == "count":
        await ask(call.message, state, "window")
    elif current in ("same_day",):
        await ask(call.message, state, "count")
    elif current in steps:
        idx = steps.index(current)
        if idx > 0:
            await ask(call.message, state, steps[idx - 1])
        elif data.get("pi", 0) > 0:  # back to the previous person's last answer
            k = data["pi"] - 1
            await load_person(state, k)
            await state.update_data(people=(data.get("people") or [])[:k + 1])
            await ask(call.message, state, steps[-1])
        else:
            await ask(call.message, state, "count")
    elif current == "phone":
        await load_person(state, max(0, len(data.get("people") or []) - 1))
        await state.update_data(people=(data.get("people") or [])[:-1] or [])
        await ask(call.message, state, steps[-1])
    else:
        await call.message.answer("Анкета уже закрыта. Начать заново — /order")


@router.callback_query(F.data == "ord:any", NewOrder.window)
async def order_any(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.update_data(date_from=None, date_to=None)
    await call.answer("Любые даты")
    await next_step(call.message, state, "window", db, settings)


@router.message(Command("cancel"), StateFilter("*"))
async def cancel(message: Message, state: FSMContext) -> None:
    had = await state.get_state()
    await state.clear()
    await message.answer("Отменено." if had else "Нечего отменять. Команды — /help")


@router.message(NewOrder.window, F.text, NOT_A_COMMAND)
async def order_window(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    from .parsing import parse_range
    kind, d1, d2 = parse_range(message.text)
    if kind == "error":
        await message.answer("Не получилось разобрать даты 🙈\nПопробуй так: <code>ноябрь</code>, <code>конец ноября</code>, "
                             "<code>5-20 ноября</code>, <code>ближайшие 2 недели</code> или нажми «Любые даты».",
                             reply_markup=step_kb("window"))
        return
    today = date.today()
    note = ""
    if kind == "ok":
        if d2 and d2 < d1:
            d1, d2 = d2, d1
        if (d2 or d1) < today:
            await message.answer("Эти даты уже прошли. Пришли даты в будущем.", reply_markup=step_kb("window"))
            return
        if d1 < today:
            d1, note = today, f"Начало сдвинул на сегодня ({fmt_date(today)}). "
        await state.update_data(date_from=d1.isoformat(), date_to=d2.isoformat() if d2 else None)
        window = f"{fmt_date(d1)}–{fmt_date(d2)}" if d2 else f"с {fmt_date(d1)}"
        await message.answer(f"{note}Даты подачи: <b>{window}</b> ✓")
    else:
        await state.update_data(date_from=None, date_to=None)
        await message.answer("Даты подачи: <b>любые</b> ✓")
    await next_step(message, state, "window", db, settings)


async def _set_count(message: Message, state: FSMContext, n: int, db: Database, settings: Settings) -> None:
    await state.update_data(n=n, people=[], pi=0, **{k: None for k in PERSONAL_KEYS})
    if n > 1:
        await state.set_state(NewOrder.same_day)
        kb = InlineKeyboardBuilder()
        kb.button(text="Только в один день", callback_data="ord:sd:1")
        kb.button(text="Можно в разные дни", callback_data="ord:sd:0")
        kb.adjust(1)
        await message.answer(f"Вас {n}. Записать всех в один день? Так удобнее — одна поездка в центр, "
                             "но такие слоты появляются реже.", reply_markup=kb.as_markup())
        return
    await ask(message, state, personal_steps((await state.get_data())["mission"])[0])


@router.callback_query(F.data.startswith("ord:n:"), NewOrder.count)
async def order_count(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await call.answer()
    await _set_count(call.message, state, int(call.data.split(":")[2]), db, settings)


@router.message(NewOrder.count, F.text, NOT_A_COMMAND)
async def order_count_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    m = re.search(r"\d+", message.text)
    n = int(m.group(0)) if m else (1 if re.search(r"\bя\b|один|одна|сам", message.text.lower()) else 0)
    if not 1 <= n <= MAX_PEOPLE:
        await message.answer(f"Выбери кнопкой: от 1 до {MAX_PEOPLE} человек. Больше — оформи вторую заявку.")
        return
    await _set_count(message, state, n, db, settings)


@router.callback_query(F.data.startswith("ord:sd:"), NewOrder.same_day)
async def order_same_day(call: CallbackQuery, state: FSMContext) -> None:
    same = call.data.endswith("1")
    await state.update_data(same_day=same)
    await call.answer()
    await call.message.answer("Ок, только в один день ✓" if same else "Ок, можно в разные дни ✓")
    await ask(call.message, state, personal_steps((await state.get_data())["mission"])[0])


@router.message(NewOrder.full_name, F.text, NOT_A_COMMAND)
async def order_name(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    from .parsing import translit
    raw = re.sub(r"\s+", " ", message.text.strip())
    if re.search(r"[а-яё]", raw, re.I):
        guess = translit(raw)
        await message.answer(f"Нужно латиницей, как в паспорте. Если так — <code>{html.escape(guess)}</code>, "
                             "скопируй и пришли (или напиши как в паспорте).", reply_markup=step_kb("full_name"))
        return
    # VFS accepts only latin letters and single spaces: hyphens/apostrophes become spaces
    name = re.sub(r"\s+", " ", re.sub(r"[-'’]", " ", raw.upper())).strip()
    if not re.fullmatch(r"[A-Z]+( [A-Z]+)+", name) or len(name) > 80:
        await message.answer("Нужны имя <b>и</b> фамилия латиницей. Пример: <code>IVAN IVANOV</code>",
                             reply_markup=step_kb("full_name"))
        return
    await state.update_data(full_name=name)
    await next_step(message, state, "full_name", db, settings)


@router.message(NewOrder.birth, F.text, NOT_A_COMMAND)
async def order_birth(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    d = parse_d(message.text)
    if not d or not (date(1920, 1, 1) < d < date.today()):
        await message.answer("Не похоже на дату рождения. Пример: <code>15.03.1990</code>", reply_markup=step_kb("birth"))
        return
    await state.update_data(birth=d.isoformat())
    await next_step(message, state, "birth", db, settings)


COMMON_NATIONALITIES = ["Russia", "Ukraine", "Belarus", "Kazakhstan", "Uzbekistan", "Armenia"]


def nationality_kb(suggestions: list[str]):
    kb = InlineKeyboardBuilder()
    for name in list(dict.fromkeys(suggestions + COMMON_NATIONALITIES))[:8]:
        kb.button(text=name, callback_data=f"ord:nat:{name}")
    kb.button(text="⬅️ Назад", callback_data="ord:back")
    kb.button(text="✖️ Отмена", callback_data="ord:cancel")
    kb.adjust(2)
    return kb.as_markup()


@router.message(NewOrder.nationality, F.text, NOT_A_COMMAND)
async def order_nat(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    from .parsing import normalize_country
    country, suggestions = normalize_country(message.text)
    if not country:
        await message.answer("Не нашёл такую страну. Выбери кнопкой или напиши по-английски, как в паспорте "
                             "(например <code>Russia</code>).", reply_markup=nationality_kb(suggestions[:2]))
        return
    await state.update_data(nationality=country)
    await message.answer(f"Гражданство: <b>{html.escape(country)}</b> ✓")
    await next_step(message, state, "nationality", db, settings)


@router.callback_query(F.data.startswith("ord:nat:"), NewOrder.nationality)
async def order_nat_button(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    country = call.data.split(":", 2)[2]
    await state.update_data(nationality=country)
    await call.answer(country)
    await call.message.answer(f"Гражданство: <b>{html.escape(country)}</b> ✓")
    await next_step(call.message, state, "nationality", db, settings)


@router.message(NewOrder.passport, F.text, NOT_A_COMMAND)
async def order_passport(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    num = re.sub(r"[\s\-№#]", "", message.text.strip().upper())
    if not re.fullmatch(r"[A-Z0-9]{6,12}", num) or sum(ch.isdigit() for ch in num) < 5:
        await message.answer("Это не похоже на номер загранпаспорта: обычно это 6–12 знаков, в основном цифры "
                             "(например <code>751234567</code> или <code>AB1234567</code>). Проверь по паспорту.",
                             reply_markup=step_kb("passport"))
        return
    await state.update_data(passport=num)
    await next_step(message, state, "passport", db, settings)


@router.message(NewOrder.passport_exp, F.text, NOT_A_COMMAND)
async def order_pexp(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    d = parse_d(message.text)
    if not d:
        await message.answer("Не похоже на дату. Пример: <code>01.01.2031</code>", reply_markup=step_kb("passport_exp"))
        return
    if d < date.today():
        await message.answer("Паспорт уже истёк — с ним визу не подать. Проверь дату.", reply_markup=step_kb("passport_exp"))
        return
    if d > date.today() + timedelta(days=10 * 366 + 30):
        await message.answer("Загранпаспорт не выдают больше чем на 10 лет — проверь дату окончания срока действия.",
                             reply_markup=step_kb("passport_exp"))
        return
    if d < date.today() + timedelta(days=180):
        await message.answer("⚠️ Паспорт должен действовать минимум 3 месяца после возвращения из поездки — "
                             "с таким сроком визу, скорее всего, не дадут. Проверь дату или сначала обнови паспорт.",
                             reply_markup=step_kb("passport_exp"))
        return
    await state.update_data(passport_exp=d.isoformat())
    await next_step(message, state, "passport_exp", db, settings)


@router.callback_query(F.data.startswith("ord:g:"), NewOrder.gender)
async def order_gender(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    g = call.data.split(":")[2]
    await state.update_data(gender=g)
    await call.answer()
    await call.message.answer(f"Пол: <b>{'мужской' if g == 'M' else 'женский'}</b> ✓")
    await next_step(call.message, state, "gender", db, settings)


@router.message(NewOrder.gender, F.text, NOT_A_COMMAND)
async def order_gender_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    t = message.text.strip().lower()
    g = "M" if t in ("м", "муж", "мужской", "m", "male") else "F" if t in ("ж", "жен", "женский", "f", "female") else None
    if not g:
        await message.answer("Выбери кнопкой: мужской или женский.", reply_markup=step_kb("gender"))
        return
    await state.update_data(gender=g)
    await next_step(message, state, "gender", db, settings)


@router.message(NewOrder.passport_scan, F.photo | F.document)
async def order_passport_scan(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    from aiogram.types import BufferedInputFile
    from .sanitize import clean_file
    if message.document and not ((message.document.mime_type or "").startswith("image/")
                                 or (message.document.mime_type or "") == "application/pdf"):
        await message.answer("Нужно фото или PDF разворота паспорта.", reply_markup=step_kb("passport_scan"))
        return
    if message.photo:
        file_id, mime, name = message.photo[-1].file_id, "image/jpeg", "passport.jpg"
    else:
        file_id, mime, name = message.document.file_id, message.document.mime_type, message.document.file_name
    raw = await message.bot.download(file_id)
    data, new_name = clean_file(raw.read(), mime, name, "passport")
    # re-upload the cleaned copy into the admin chat so the stored file has no metadata
    admin_id = next(iter(settings.admin_ids))
    stored = await message.bot.send_document(admin_id, BufferedInputFile(data, filename=new_name),
                                             caption="📎 скан паспорта (заявка оформляется)", disable_notification=True)
    await state.update_data(passport_scan=stored.document.file_id)
    await message.answer("Скан получен ✓")
    await next_step(message, state, "passport_scan", db, settings)


@router.message(NewOrder.residence_exp, F.text, NOT_A_COMMAND)
async def order_residence(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    d = parse_d(message.text)
    if not d:
        await message.answer("Не похоже на дату. Пример: <code>15.08.2027</code>", reply_markup=step_kb("residence_exp"))
        return
    data = await state.get_data()
    trip_end = date.fromisoformat(data["date_to"]) if data.get("date_to") else (
        date.fromisoformat(data["date_from"]) if data.get("date_from") else date.today()) + timedelta(days=30)
    need = trip_end + timedelta(days=90)
    if d < need:
        await message.answer(
            f"⚠️ Для визы ВНЖ должен действовать минимум 3 месяца после возвращения — примерно до "
            f"<b>{fmt_date(need)}</b> или дольше. С ВНЖ до {fmt_date(d)} в визе, скорее всего, откажут.\n"
            "Если ВНЖ сейчас на продлении — нажми кнопку ниже (Австрия, например, принимает ресипт + старый ВНЖ).",
            reply_markup=step_kb("residence_exp"))
        return
    await state.update_data(residence_exp=d.isoformat(), residence_receipt=False)
    await next_step(message, state, "residence_exp", db, settings)


@router.callback_query(F.data == "ord:receipt", NewOrder.residence_exp)
async def order_receipt(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.update_data(residence_exp=None, residence_receipt=True)
    await call.answer()
    await call.message.answer("Отметил: ВНЖ на продлении. Админ проверит, принимает ли страна ресипт.")
    await next_step(call.message, state, "residence_exp", db, settings)


@router.message(NewOrder.phone, F.text, NOT_A_COMMAND)
async def order_phone(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    phone = re.sub(r"[^\d+]", "", message.text)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if re.fullmatch(r"\d{8}", phone) and phone[0] in "29":  # local Cyprus number
        phone = "+357" + phone
    if not re.fullmatch(r"\+?\d{8,15}", phone):
        await message.answer("Не похоже на номер. Пример: <code>+35799123456</code>", reply_markup=step_kb("phone"))
        return
    await state.update_data(phone=phone if phone.startswith("+") else "+" + phone)
    await next_step(message, state, "phone", db, settings)


@router.message(NewOrder.email, F.text, NOT_A_COMMAND)
async def order_email(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    email = re.sub(r"\s+", "", message.text).lower()
    if not EMAIL_RE.match(email):
        await message.answer("Не похоже на email. Пример: <code>ivan@gmail.com</code>", reply_markup=step_kb("email"))
        return
    await state.update_data(email=email)
    await next_step(message, state, "email", db, settings)


def person_lines(p: dict, numbered: int | None = None) -> str:
    head = f"\n👤 <b>{numbered}. {html.escape(p.get('full_name') or '')}</b>\n" if numbered else \
        f"Имя: {html.escape(p.get('full_name') or '')}\n"
    return (head
            + (f"Дата рождения: {fmt_date(date.fromisoformat(p['birth']))}\n" if p.get("birth") else "")
            + f"Гражданство: {html.escape(p.get('nationality') or '')}\n"
            + f"Паспорт: {html.escape(p.get('passport') or '')}"
            + (f" до {fmt_date(date.fromisoformat(p['passport_exp']))}" if p.get("passport_exp") else "") + "\n"
            + (f"Пол: {'мужской' if p.get('gender') == 'M' else 'женский'}\n" if p.get("gender") else "")
            + (f"ВНЖ до: {fmt_date(date.fromisoformat(p['residence_exp']))}\n" if p.get("residence_exp")
               else ("ВНЖ: на продлении (ресипт)\n" if p.get("residence_receipt") else "")))


async def show_confirm(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    people = data.get("people") or []
    n = max(1, len(people))
    price = await db.price(data["mission"], settings.default_price)
    fee = await db.fee(data["mission"])
    if data.get("date_from"):
        d1 = date.fromisoformat(data["date_from"])
        window = f"{fmt_date(d1)}–{fmt_date(date.fromisoformat(data['date_to']))}" if data.get("date_to") else f"с {fmt_date(d1)}"
    else:
        window = "любые"
    each = f" × {n} = <b>{price * n:.0f} €</b>" if n > 1 else ""
    fee_each = f" × {n} = <b>{fee * n:.2f} €</b>" if n > 1 else ""
    text = (
        "<b>Проверь заявку</b>\n"
        f"Страна: {html.escape(data['mission_name'])} ({html.escape(data.get('note', ''))})\n"
        f"Даты подачи: {window}\n"
        + (f"Человек: {n}" + (", все в один день" if data.get("same_day") else ", можно в разные дни") + "\n" if n > 1 else "")
        + ("".join(person_lines(p, i + 1) for i, p in enumerate(people)) + "\n" if n > 1 else person_lines(people[0] if people else {}))
        + (f"Телефон (для визового центра): {html.escape(data['phone'])}\n" if data.get("phone") else "")
        + (f"Связь: Telegram {html.escape(data['tg_contact'])} — пишем сюда, в этот чат\n\n" if data.get("tg_contact")
           else "Связь: пишем сюда, в этот чат\n\n")
        + (f"💳 Сбор визового центра: {fee:.2f} €{fee_each or f' — <b>{fee:.2f} €</b>'} — сразу после заявки, без него слот не забронировать\n"
           f"🤝 Наш сервис: {price:.0f} €{each or f' — <b>{price:.0f} €</b>'} — после брони\n"
           f"Итого: <b>{(fee + price) * n:.2f} €</b>\n\n" if fee else
           f"Стоимость сервиса: {price:.0f} €" + (each if n > 1 else "") + " (после брони)\n\n")
        + (f"🏛 В день подачи каждый оплачивает сам: {requirements(data['mission']).at_centre}."
           + (" Для детей консульский сбор меньше: 6–12 лет — 45 €, до 6 лет — бесплатно." if _has_child(people) else "")
           + "\n\n" if requirements(data["mission"]).at_centre else "")
        + CONSENT + (FEE_NOTE if fee else "")
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить заявку", callback_data="ord:confirm")
    kb.button(text="✏️ Исправить", callback_data="ord:edit")
    kb.button(text="✖️ Отмена", callback_data="ord:cancel")
    kb.adjust(1, 2)
    await state.set_state(NewOrder.confirm)
    await message.answer(text, reply_markup=kb.as_markup())


EDIT_LABELS = {"window": "Даты", "full_name": "Имя", "gender": "Пол", "birth": "Дата рождения",  # noqa
               "nationality": "Гражданство", "passport": "Номер паспорта", "passport_exp": "Срок паспорта",
               "passport_scan": "Скан паспорта", "residence_exp": "ВНЖ", "phone": "Телефон", "email": "Email"}


@router.callback_query(F.data == "ord:edit", NewOrder.confirm)
async def order_edit(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    people = data.get("people") or []
    kb = InlineKeyboardBuilder()
    kb.button(text="Даты", callback_data="ord:fix:window")
    if "phone" in form_steps(data.get("mission", "")):
        kb.button(text="Телефон", callback_data="ord:fix:phone")
    if len(people) > 1:
        for i, p in enumerate(people):
            kb.button(text=f"👤 {p.get('full_name') or i + 1}", callback_data=f"ord:fixp:{i}")
    else:
        for step in personal_steps(data.get("mission", "")):
            kb.button(text=EDIT_LABELS.get(step, step), callback_data=f"ord:fixf:0:{step}")
    kb.adjust(2)
    await call.message.answer("Что исправить?", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("ord:fixp:"), NewOrder.confirm)
async def order_fix_person(call: CallbackQuery, state: FSMContext) -> None:
    k = int(call.data.split(":")[2])
    data = await state.get_data()
    p = (data.get("people") or [{}])[k]
    kb = InlineKeyboardBuilder()
    for step in personal_steps(data.get("mission", "")):
        kb.button(text=EDIT_LABELS.get(step, step), callback_data=f"ord:fixf:{k}:{step}")
    kb.adjust(2)
    await call.message.answer(f"Что исправить у {html.escape(p.get('full_name') or str(k + 1))}?", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("ord:fixf:"), NewOrder.confirm)
async def order_fix_field(call: CallbackQuery, state: FSMContext) -> None:
    _, _, k, step = call.data.split(":")
    await load_person(state, int(k))
    await state.update_data(editing="field")
    await call.answer()
    await ask(call.message, state, step)


@router.callback_query(F.data.startswith("ord:fix:"), NewOrder.confirm)
async def order_fix(call: CallbackQuery, state: FSMContext) -> None:
    step = call.data.split(":")[2]
    await state.update_data(editing=True)
    await call.answer()
    await ask(call.message, state, step)


@router.message(StateFilter(NewOrder.confirm), NOT_A_COMMAND)
async def order_confirm_text(message: Message) -> None:
    await message.answer("Нажми «✅ Отправить заявку», «✏️ Исправить» или «✖️ Отмена» под анкетой выше.")


_confirm_locks: dict[int, asyncio.Lock] = {}


@router.callback_query(F.data == "ord:confirm", NewOrder.confirm)
async def order_confirm(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    lock = _confirm_locks.setdefault(call.from_user.id, asyncio.Lock())
    if lock.locked():
        await call.answer()
        return
    async with lock:
        if await state.get_state() != NewOrder.confirm.state:
            await call.answer("Заявка уже отправлена.")
            return
        await _order_confirm(call, state, db, settings)


async def _order_confirm(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    await state.clear()
    reason = await order_block_reason(db, settings, call.from_user.id, data["mission"])
    if reason:  # e.g. two forms filled in parallel
        await call.message.edit_text(reason)
        await call.answer()
        return
    people = data.get("people") or []
    n = max(1, len(people))
    applicant = {"people": people, "phone": data.get("phone"), "tg_contact": data.get("tg_contact"),
                 "same_day": bool(data.get("same_day")) if n > 1 else None}
    d_from = date.fromisoformat(data["date_from"]) if data.get("date_from") else None
    d_to = date.fromisoformat(data["date_to"]) if data.get("date_to") else None
    fee = await db.fee(data["mission"]) * n
    order_id = await db.create_order(call.from_user.id, data["mission"], d_from, d_to, security.encrypt(applicant),
                                     destination=data.get("destination"), fee=fee, people=n)
    await db.log_event(call.from_user.id, "order_new", f"#{order_id} {data.get('destination')}")
    await call.answer()
    if fee > 0:
        await call.message.edit_text(f"✅ Заявка <b>#{order_id}</b> принята: {html.escape(data['mission_name'])}.")
        await issue_fee_invoice(call.bot, db, settings, order_id)
        header = f"💳 Новая заявка{f' на {n} чел.' if n > 1 else ''} — ждём оплату сбора {fee:.2f} €. Бронировать пока рано."
    else:
        await call.message.edit_text(
            f"✅ Заявка <b>#{order_id}</b> принята: {html.escape(data['mission_name'])}.\n"
            "Как только поймаем слот и запишем тебя — пришлю подтверждение и детали. Статус — /myorders")
        header = "🆕 Новая заявка" + (f" на {n} чел." if n > 1 else "")
    for admin_id in settings.admin_ids:
        await send_admin_card(call.bot, db, admin_id, order_id, settings, header=header)


@router.callback_query(F.data.in_({"ord:confirm", "ord:edit", "ord:any"}) | F.data.startswith("ord:fix:"))
async def stale_order_button(call: CallbackQuery) -> None:
    await call.answer("Эта анкета уже закрыта. Начать заново — /order", show_alert=True)


@router.message(StateFilter(NewOrder), NOT_A_COMMAND)
async def order_unexpected(message: Message, state: FSMContext) -> None:
    """Sticker, photo, voice... in the middle of the form: ask the same question again."""
    step = (await state.get_state() or "").split(":")[-1]
    if step == "passport_scan":
        await message.answer("Нужна фотография разворота паспорта 📷", reply_markup=step_kb(step))
    elif step in QUESTIONS:
        await message.answer("Нужен текстом 🙂\n\n" + QUESTIONS[step], reply_markup=step_kb(step))


# ---------------- client: my orders ----------------
@router.message(Command("myorders"))
async def my_orders(message: Message, db: Database, uid: int | None = None) -> None:
    rows = await db.orders_list(tuple(STATUS), tg_id=uid or message.from_user.id, limit=10)
    if not rows:
        await message.answer("Заявок нет. Оформить — /order")
        return
    lines, kb = ["<b>Мои заявки</b>"], InlineKeyboardBuilder()
    for r in rows:
        lines.append(f"#{r['id']} {html.escape(order_country(r))}" + (f" · {r['people']} чел." if r["people"] > 1 else "")
                     + f" — {STATUS.get(r['status'], r['status'])}"
                     + (f"\n     запись: {html.escape(r['appointment'])}" if r["appointment"] else ""))
        if r["status"] == "booked" and r["invoice_url"]:
            kb.button(text=f"Оплатить #{r['id']} — {r['price_eur']:.0f} €", url=r["invoice_url"])
        if r["status"] == "awaiting_fee" and r["fee_invoice_url"]:
            kb.button(text=f"💳 Оплатить сбор #{r['id']} — {r['fee_eur']:.2f} €", url=r["fee_invoice_url"])
        if r["status"] in ("awaiting_fee", "new", "in_progress"):
            kb.button(text=f"✖️ Отменить #{r['id']}", callback_data=f"myo:cancel:{r['id']}")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("myo:cancel:"))
async def client_cancel(call: CallbackQuery, db: Database, settings: Settings) -> None:
    oid = int(call.data.split(":")[2])
    o = await db.order(oid)
    if not o or o["tg_id"] != call.from_user.id:
        await call.answer("Нет такой заявки", show_alert=True)
        return
    if o["status"] not in ("awaiting_fee", "new", "in_progress"):
        await call.answer("Слот уже забронирован — отменить можно только через /support.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отменить", callback_data=f"myo:yes:{oid}")
    kb.button(text="Нет", callback_data="myo:no:0")
    await call.message.answer(f"Отменить заявку #{oid}?", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("myo:yes:") | (F.data == "myo:no:0"))
async def client_cancel_confirm(call: CallbackQuery, db: Database, settings: Settings) -> None:
    if call.data == "myo:no:0":
        await call.message.edit_text("Ок, заявка остаётся.")
        await call.answer()
        return
    oid = int(call.data.split(":")[2])
    o = await db.order(oid)
    if not o or o["tg_id"] != call.from_user.id or o["status"] not in ("awaiting_fee", "new", "in_progress"):
        await call.answer("Уже нельзя отменить — напиши в /support.", show_alert=True)
        return
    await cancel_order(call.bot, db, settings, o, by_client=True)
    await call.message.edit_text(f"Заявка #{oid} отменена."
                                 + (" Сбор вернём в течение пары дней." if o["fee_paid_at"] else ""))
    await call.answer()


# ---------------- admin ----------------
def order_country(o) -> str:
    dest = o["destination"] if "destination" in o.keys() else None
    if dest in DESTINATIONS:
        name, _, note = DESTINATIONS[dest]
        return f"{name} ({note})"
    return o["mission_name"] or o["mission"]


def admin_buttons(o) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    oid, st = o["id"], o["status"]
    if st == "awaiting_fee":
        kb.button(text="💳 Сбор оплачен вручную", callback_data=f"oa:feepaid:{oid}")
    if st == "new":
        kb.button(text="🔎 Взять в работу", callback_data=f"oa:take:{oid}")
    if st in ("new", "in_progress"):
        kb.button(text="📅 Забронировано", callback_data=f"oa:book:{oid}")
    if st == "booked":
        kb.button(text="💰 Оплачено вручную", callback_data=f"oa:paid:{oid}")
        kb.button(text="🔁 Новый счёт", callback_data=f"oa:invoice:{oid}")
    if st == "paid":
        kb.button(text="✅ Завершить", callback_data=f"oa:done:{oid}")
    if st in ("awaiting_fee", "new", "in_progress", "booked"):
        kb.button(text="✖️ Отменить", callback_data=f"oa:cancel:{oid}")
    kb.adjust(2)
    return kb


def _d(iso: str | None) -> str:
    try:
        return fmt_date(date.fromisoformat(iso)) if iso else "—"
    except ValueError:
        return iso


def applicant_people(a: dict) -> list[dict]:
    """New orders keep a list of people; older ones stored a single applicant at the top level."""
    return a.get("people") or [a]


def admin_card_text(o, tz: ZoneInfo) -> str:
    a = security.decrypt(o["applicant"])
    people = applicant_people(a)
    window = f"{fmt_date(o['date_from'])}–{fmt_date(o['date_to'])}" if o["date_from"] else "любые"
    lines = [
        f"<b>Заявка #{o['id']}</b> · {STATUS.get(o['status'], o['status'])}",
        f"Страна: <b>{html.escape(order_country(o))}</b> · даты: {window}",
        f"Клиент: {o['user_name'] or '—'}" + (f" @{html.escape(o['username'])}" if o["username"] else "")
        + f" (<code>{o['tg_id']}</code>)",
        f"Создана: {o['created_at'].astimezone(tz).strftime('%d.%m %H:%M')}",
    ]
    if len(people) > 1:
        lines.append(f"Человек: <b>{len(people)}</b>" + (" · только в один день" if a.get("same_day") else " · можно в разные дни"))
    for i, p in enumerate(people, 1):
        lines.append("")
        lines.append((f"{i}. " if len(people) > 1 else "") + f"<code>{html.escape(p.get('full_name') or '')}</code>"
                     + (f" · {p['gender']}" if p.get("gender") else ""))
        lines.append(" · ".join(x for x in [
            f"рожд. {_d(p.get('birth'))}" if p.get("birth") else "", html.escape(p.get("nationality") or "")] if x))
        lines.append(f"Паспорт: <code>{html.escape(p.get('passport') or '')}</code>"
                     + (f" до {_d(p.get('passport_exp'))}" if p.get("passport_exp") else ""))
        if p.get("residence_exp") or p.get("residence_receipt"):
            lines.append(f"ВНЖ: до {_d(p['residence_exp'])}" if p.get("residence_exp") else "ВНЖ: ⚠️ на продлении (ресипт)")
    lines.append("")
    lines.append((f"Тел: <code>{html.escape(a['phone'])}</code>" if a.get("phone") else "Тел: —")
                 + (f" · <code>{html.escape(a['email'])}</code>" if a.get("email") else ""))
    if o["appointment"]:
        lines.append(f"\nЗапись: <b>{html.escape(o['appointment'])}</b>")
    if o["fee_eur"] and float(o["fee_eur"]) > 0:
        lines.append(f"Сбор: {o['fee_eur']:.2f} € — " + (f"✅ оплачен ({html.escape(o['fee_paid'] or '')})"
                                                          if o["fee_paid_at"] else "⏳ не оплачен"))
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
        msg = await bot.send_message(chat_id, text, reply_markup=admin_buttons(o).as_markup())
        for p in applicant_people(security.decrypt(o["applicant"])):
            if p.get("passport_scan"):
                await bot.send_document(chat_id, p["passport_scan"], reply_to_message_id=msg.message_id,
                                        caption=f"Скан паспорта: {p.get('full_name') or ''}, заявка #{o['id']}")
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
        lines.append(f"#{r['id']} {html.escape(order_country(r))} · {window} · {STATUS[r['status']]}")
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
    if action == "bookcancel":
        await state.clear()
        await call.message.edit_text("Ок, бронь не вносим.")
        await call.answer()
        return
    o = await db.order(oid)
    if not o:
        await call.answer("Нет такой заявки", show_alert=True)
        return
    allowed = {"take": ("new",), "book": ("new", "in_progress"), "paid": ("booked",), "invoice": ("booked",),
               "done": ("paid",), "cancel": ("awaiting_fee", "new", "in_progress", "booked"),
               "feepaid": ("awaiting_fee",)}
    if action in allowed and o["status"] not in allowed[action]:
        await call.answer(f"Нельзя: заявка уже в статусе «{STATUS.get(o['status'], o['status'])}». "
                          "Открой свежую карточку: /orders", show_alert=True)
        return
    if action == "open":
        await send_admin_card(call.bot, db, call.message.chat.id, oid, settings)
    elif action == "take":
        await db.update_order(oid, status="in_progress")
        await notify_client(call.bot, o["tg_id"], f"🔎 Заявка #{oid}: начали искать слот.")
    elif action == "book":
        await state.set_state(Booking.details)
        await state.update_data(order_id=oid)
        price = (o["price_eur"] if o["price_eur"] is not None
                 else await db.price(o["mission"], settings.default_price) * (o["people"] or 1))
        cancel_kb = InlineKeyboardBuilder()
        cancel_kb.button(text="✖️ Не сейчас", callback_data="oa:bookcancel:0")
        await call.message.answer(reply_markup=cancel_kb.as_markup(), text=
            f"Заявка #{oid}. Пришли <b>одним сообщением</b>:\n"
            "• PDF подтверждения записи файлом, а в подписи — дату, время и место\n"
            "  или просто текст, если PDF нет.\n"
            f"Пример подписи: <code>28.10.2026 10:00, VFS Nicosia</code>\n"
            f"Цена по умолчанию {price:.0f} €. Другая цена — допиши в конце: <code>… ; 120</code>")
    elif action == "paid":
        if o["invoice_id"] and payments.enabled():
            await payments.delete_invoice(o["invoice_id"])
        await mark_paid(call.bot, db, settings, oid, asset="manual", amount=str(o["price_eur"] or ""))
    elif action == "invoice":
        await issue_invoice(call.bot, db, settings, oid)
    elif action == "done":
        await db.update_order(oid, status="done")
    elif action == "cancel":
        await cancel_order(call.bot, db, settings, o, by_client=False)
    elif action == "feepaid":
        if o["fee_invoice_id"] and payments.enabled():
            await payments.delete_invoice(o["fee_invoice_id"])
        await fee_received(call.bot, db, settings, oid, "manual", str(o["fee_eur"]))
    if action in ("take", "done", "cancel", "paid", "feepaid"):
        o = await db.order(oid)
        try:
            await call.message.edit_text(admin_card_text(o, ZoneInfo(settings.tz)),
                                         reply_markup=admin_buttons(o).as_markup())
        except Exception:
            pass
    await call.answer()


@router.message(Booking.details, F.document | (F.text & NOT_A_COMMAND))
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
    from .parsing import parse_date
    if not appointment or not parse_date(appointment):
        await message.answer("Не вижу даты записи. Нужна дата и время, например <code>28.10.2026 10:00, VFS Nicosia</code> "
                             "(в тексте или в подписи к PDF). Передумал — кнопка «Не сейчас» выше.")
        return
    o = await db.order(oid)
    if o["status"] not in ("new", "in_progress"):
        await state.clear()
        await message.answer(f"Заявка #{oid} уже в статусе «{STATUS.get(o['status'], o['status'])}», бронь не вношу.")
        return
    if price is None:
        price = (float(o["price_eur"]) if o["price_eur"] is not None
                 else await db.price(o["mission"], settings.default_price) * (o["people"] or 1))
    file_id = None
    if message.document:
        # strip metadata (author, software, EXIF) and rename before the client ever sees the file
        from aiogram.types import BufferedInputFile
        from .sanitize import clean_file
        raw = await message.bot.download(message.document.file_id)
        data, name = clean_file(raw.read(), message.document.mime_type, message.document.file_name, f"booking_{oid}")
        sent = await message.answer_document(BufferedInputFile(data, filename=name),
                                             caption="🧹 Так файл получит клиент: метаданные очищены, имя нейтральное.")
        file_id = sent.document.file_id
    await db.update_order(oid, status="booked", appointment=appointment[:200], price_eur=price,
                          confirmation_file=file_id)
    await state.clear()
    await db.log_event(o["tg_id"], "order_booked", f"#{oid}")
    await issue_invoice(message.bot, db, settings, oid)
    await message.answer(f"Готово: заявка #{oid} забронирована, клиенту отправлен счёт на {price:.0f} €.")


# ---------------- money ----------------
async def notify_client(bot: Bot, uid: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except Exception:
        log.warning("cannot message client %s", uid)
        return False


async def issue_invoice(bot: Bot, db: Database, settings: Settings, oid: int) -> None:
    o = await db.order(oid)
    name = order_country(o)
    from .documents import links_html
    docs = links_html(o["mission"], o["destination"])
    head = (f"✅ <b>Слот забронирован!</b>\nЗаявка #{oid}: {html.escape(name)}\n"
            f"Запись: <b>{html.escape(o['appointment'] or '')}</b>\n\n"
            + (f"📄 Что взять на подачу:\n{docs}\n\n" if docs else ""))
    kb = InlineKeyboardBuilder()
    if payments.enabled():
        if o["invoice_id"]:
            await payments.delete_invoice(o["invoice_id"])  # the old link must not stay payable
        try:
            invoice_id, url = await payments.create_invoice(oid, float(o["price_eur"]), f"Visa slot service, order #{oid}, {name}")
            await db.update_order(oid, invoice_id=invoice_id, invoice_url=url)
            kb.button(text=f"💳 Оплатить {o['price_eur']:.0f} € криптой", url=url)
            pay = "Оплати сервис по кнопке ниже (USDT, TON, BTC и др.). Как только оплата пройдёт, бот это увидит."
        except payments.PaymentError as e:
            log.error("invoice failed: %s", e)
            pay = f"Стоимость: {o['price_eur']:.0f} €. Реквизиты для оплаты пришлёт поддержка."
            for a in settings.admin_ids:
                await notify_client(bot, a, f"⚠️ Не удалось создать счёт для #{oid}: <code>{html.escape(str(e))}</code>")
    else:
        pay = f"Стоимость: {o['price_eur']:.0f} €. Реквизиты для оплаты пришлёт поддержка."
    pdf_note = "\n\nPDF подтверждения пришлю сразу после оплаты." if (o["confirmation_file"] and settings.pdf_after_payment) else ""
    reached = await notify_client(bot, o["tg_id"], head + pay + pdf_note,
                                  reply_markup=kb.as_markup() if payments.enabled() else None)
    if not reached:
        for a in settings.admin_ids:
            await notify_client(bot, a, f"⚠️ Клиент заявки #{oid} недоступен (заблокировал бота или удалил чат). "
                                        "Счёт создан, но сообщение не доставлено.")
    if o["confirmation_file"] and not settings.pdf_after_payment:
        try:
            await bot.send_document(o["tg_id"], o["confirmation_file"], caption=f"Подтверждение записи, заявка #{oid}")
        except Exception:
            log.warning("cannot send pdf for %s", oid)


async def issue_fee_invoice(bot: Bot, db: Database, settings: Settings, oid: int) -> None:
    o = await db.order(oid)
    fee = float(o["fee_eur"])
    text = (f"💳 Заявка #{oid}: чтобы мы могли забронировать слот, оплати <b>сбор визового центра {fee:.2f} €</b>.\n"
            "Его мы платим при бронировании. Как только оплата пройдёт — начнём искать слот.\n"
            "Счёт действует 48 часов.")
    kb = InlineKeyboardBuilder()
    if payments.enabled():
        try:
            invoice_id, url = await payments.create_invoice(oid, fee, f"Visa centre fee, order #{oid}",
                                                            expires_in=48 * 3600)
            await db.update_order(oid, fee_invoice_id=invoice_id, fee_invoice_url=url)
            kb.button(text=f"💳 Оплатить сбор {fee:.2f} €", url=url)
        except payments.PaymentError as e:
            log.error("fee invoice failed: %s", e)
            text = f"Заявка #{oid}: нужно оплатить сбор визового центра {fee:.2f} €. Реквизиты пришлёт поддержка."
    else:
        text = f"Заявка #{oid}: нужно оплатить сбор визового центра {fee:.2f} €. Реквизиты пришлёт поддержка."
    await notify_client(bot, o["tg_id"], text, reply_markup=kb.as_markup() if payments.enabled() else None)


async def fee_received(bot: Bot, db: Database, settings: Settings, oid: int, asset: str, amount: str) -> None:
    o = await db.order(oid)
    if not o or o["status"] != "awaiting_fee":
        return
    await db.update_order(oid, status="new", fee_paid_at=datetime.now(timezone.utc), fee_paid=f"{amount} {asset}")
    await db.log_event(o["tg_id"], "fee_paid", f"#{oid} {amount} {asset}")
    await notify_client(bot, o["tg_id"], f"✅ Сбор по заявке #{oid} получен. Ищем слот — напишем, как только запишем тебя.")
    for a in settings.admin_ids:
        await send_admin_card(bot, db, a, oid, settings, header=f"✅ Сбор оплачен ({amount} {asset}) — можно бронировать")


async def cancel_order(bot: Bot, db: Database, settings: Settings, o, by_client: bool) -> None:
    oid = o["id"]
    for inv in (o["invoice_id"], o["fee_invoice_id"]):
        if inv and payments.enabled():
            await payments.delete_invoice(inv)
    await db.update_order(oid, status="cancelled")
    await db.log_event(o["tg_id"], "order_cancel_client" if by_client else "order_cancel", f"#{oid}")
    refund = (f"\n💸 Сбор {o['fee_eur']:.2f} € уже был оплачен ({html.escape(o['fee_paid'] or '')}) — "
              "верни его клиенту: @CryptoBot → Отправить, или чеком.") if o["fee_paid_at"] else ""
    if by_client:
        for a in settings.admin_ids:
            await notify_client(bot, a, f"✖️ Клиент сам отменил заявку #{oid} ({html.escape(order_country(o))}).{refund}")
    else:
        await notify_client(bot, o["tg_id"], f"✖️ Заявка #{oid} отменена."
                            + (" Сбор вернём в течение пары дней." if o["fee_paid_at"] else "") + " Вопросы — /support")
        if refund:
            for a in settings.admin_ids:
                await notify_client(bot, a, f"Заявка #{oid} отменена.{refund}")


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
    tick = 0
    while True:
        await asyncio.sleep(every)
        tick += 1
        try:
            if payments.enabled():
                rows = await db.awaiting_payment()
                states = await payments.invoice_statuses([r["invoice_id"] for r in rows])
                for r in rows:
                    inv = states.get(int(r["invoice_id"]))
                    if not inv:
                        continue
                    if inv.get("status") == "paid":
                        await mark_paid(bot, db, settings, r["id"], inv.get("paid_asset") or inv.get("asset") or "crypto",
                                        str(inv.get("paid_amount") or inv.get("amount") or ""))
                    elif inv.get("status") == "expired" and not r["expired_notified"]:
                        await db.update_order(r["id"], expired_notified=True)
                        for a in settings.admin_ids:
                            await notify_client(bot, a, f"⌛ Счёт по заявке #{r['id']} истёк без оплаты. "
                                                        "Напиши клиенту через бота или выставь новый (🔁 Новый счёт).")
                fee_rows = await db.awaiting_fee()
                fee_states = await payments.invoice_statuses([r["fee_invoice_id"] for r in fee_rows])
                for r in fee_rows:
                    inv = fee_states.get(int(r["fee_invoice_id"]))
                    if not inv:
                        continue
                    if inv.get("status") == "paid":
                        await fee_received(bot, db, settings, r["id"], inv.get("paid_asset") or inv.get("asset") or "crypto",
                                           str(inv.get("paid_amount") or inv.get("amount") or ""))
                    elif inv.get("status") == "expired":
                        o = await db.order(r["id"])
                        await cancel_order(bot, db, settings, o, by_client=False)
                        await notify_client(bot, r["tg_id"], f"Счёт на сбор по заявке #{r['id']} истёк, заявка закрыта. "
                                                             "Если ещё нужно — оформи заново: /order")
            if tick % 30 == 1:  # every ~30 min: housekeeping
                await auto_complete(bot, db, settings)
                await db.purge_stale_forms(hours=48)
                from .cases import ask_for_cases
                await ask_for_cases(bot, db)
            await weekly_slots_report(bot, db, settings)
        except Exception:
            log.exception("payment watcher")


async def weekly_slots_report(bot: Bot, db: Database, settings: Settings) -> None:
    """Every Monday from 10:00 (Cyprus time) admins get last week's slot statistics, once."""
    now = datetime.now(ZoneInfo(settings.tz))
    if now.weekday() != 0 or now.hour < 10:
        return
    week = now.strftime("%G-W%V")
    if await db.kv_get("weekly_slots_report") == week:
        return
    await db.kv_set("weekly_slots_report", week)
    from .slotstats import report
    text = report(await db.observations(7), 7, settings.tz, await db.first_observation())
    for a in settings.admin_ids:
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            await notify_client(bot, a, chunk)


async def auto_complete(bot: Bot, db: Database, settings: Settings) -> None:
    """Paid orders whose appointment date has passed are done: they stop counting toward the 2-country limit."""
    from .parsing import parse_date
    for r in await db.orders_list(("paid",), limit=200):
        d = parse_date(r["appointment"] or "")
        if d and d < date.today() - timedelta(days=1):
            await db.update_order(r["id"], status="done")
            log.info("order #%s auto-completed (appointment %s passed)", r["id"], d)


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
    lines = [f"<b>Цены</b> (сервис по умолчанию {settings.default_price:.0f} €)"]
    for r in await db.prices():
        fee = await db.fee(r["code"])
        lines.append(f"<code>{r['code']}</code> {html.escape(r['name'])}: сервис "
                     + (f"{r['price_eur']:.0f} €" if r["price_eur"] is not None else f"{settings.default_price:.0f} €")
                     + (f" + сбор {fee:.2f} € при брони" if fee else " · сбора при брони нет"))
    lines.append("\nИзменить: /price hun 120 · сбор: /fee svk 29 · проверить оплату: /testinvoice")
    payment_state = "подключена" if payments.enabled() else "не подключена (нужен CRYPTO_PAY_TOKEN)"
    lines.append(f"Оплата Crypto Pay: {payment_state}")
    await message.answer("\n".join(lines))


@router.message(Command("fee"))
async def fee_cmd(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    args = (command.args or "").replace(",", ".").split()
    if len(args) == 2 and re.fullmatch(r"\d+(\.\d+)?", args[1]):
        await db.set_fee(args[0].lower(), float(args[1]))
        await message.answer(f"Сбор при брони {args[0].lower()}: {float(args[1]):.2f} € (0 — сбора нет)")
    else:
        await message.answer("Формат: /fee svk 29.00 — сбор визового центра, который платится при бронировании. "
                             "0 — если сбор платится в центре. Текущие — /price")


@router.message(Command("testinvoice"))
async def test_invoice(message: Message, settings: Settings) -> None:
    """Admin: create a 1 EUR invoice to see exactly what a client sees in CryptoBot."""
    if not is_admin(message.from_user.id, settings):
        return
    if not payments.enabled():
        await message.answer("CRYPTO_PAY_TOKEN не задан.")
        return
    try:
        _, url = await payments.create_invoice(0, 1.0, "Visa slot service, test")
    except Exception as e:
        await message.answer(f"Ошибка Crypto Pay: <code>{html.escape(str(e))[:300]}</code>")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть тестовый счёт (1 €)", url=url)
    await message.answer("Открой и посмотри, что видит клиент: нет ли там твоего имени или username. "
                         "Платить не обязательно — счёт ни к чему не привязан.", reply_markup=kb.as_markup())

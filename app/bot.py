from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .db import Database
from .poller import Poller, fmt_date

router = Router()

USER_COMMANDS = [
    ("order", "Записаться на визу"),
    ("myorders", "Мои заявки и оплата"),
    ("invite", "Привести друга — скидка 50%"),
    ("wheel", "Колесо фортуны — скидка"),
    ("faq", "Частые вопросы"),
    ("terms", "Условия сервиса"),
    ("case", "Поделиться результатом визы"),
    ("support", "Написать в поддержку"),
    ("help", "Как это работает"),
]
ADMIN_COMMANDS = [
    ("admin", "Панель админа"),
    ("panel", "Кабинет в браузере"),
    ("orders", "Открытые заявки"),
    ("status", "Ближайшие даты сейчас"),
    ("history", "История слотов: /history hun"),
    ("slots", "Статистика слотов: /slots 30"),
    ("price", "Цены: /price hun 120"),
    ("fee", "Сбор при брони: /fee svk 29"),
    ("testinvoice", "Тестовый счёт на 1 €"),
    ("users", "Пользователи"),
    ("user", "Карточка: /user ID"),
    ("link", "Ссылка с меткой: /link community"),
    ("broadcast", "Рассылка: /broadcast текст"),
    ("post", "Пост в канал: /post текст (или reply)"),
    ("givediscount", "Скидка: /givediscount @user 100"),
    ("ban", "Заблокировать: /ban ID"),
    ("unban", "Разблокировать: /unban ID"),
    ("health", "Состояние проверок"),
    ("discover", "Переразведка стран VFS"),
    ("vfslogin", "Проверить автовход в VFS"),
    ("capsolver", "Проверить Capsolver: баланс+sitekey"),
    ("slotprobe", "Разведка эндпоинтов слотов"),
    ("monprobe", "Сырой ответ поиска слотов: /monprobe aut"),
    ("calprobe", "Тест реалтайм-календаря через браузер: /calprobe aut"),
    ("rkform", "Германия: показать форму записи"),
]

HELP = (
    "Помогаю получить запись на шенгенскую визу с Кипра — бронируем слот за тебя.\n\n"
    "<b>Как это работает</b>\n"
    "1. /order — выбираешь страну, даты и отправляешь данные паспорта\n"
    "2. Если визовый центр берёт сбор за запись — оплачиваешь его сразу (это не наши деньги, мы платим его за тебя)\n"
    "3. Мы ловим свободный слот и записываем тебя\n"
    "4. Присылаем подтверждение — ты оплачиваешь наш сервис в крипте\n\n"
    "💸 Сервис — {price} € с человека. Семья 2 взрослых + ребёнок — пакет {family} € за всех. "
    "Плюс сбор визового центра, если он есть. Ниже, чем у конкурентов: мы не вредители, "
    "а помогаторы. За сервис платишь только когда запись уже есть.\n\n"
    "📂 Можем собрать документы на визу полностью за тебя — {docs_price} €. Напиши в /support.\n"
    "🤝 /invite — приведи друга, получи 50% на следующую заявку. 🎡 /wheel — колесо фортуны со скидкой.\n\n"

    "<b>Страны</b>\n"
    "Австрия, Бельгия, Венгрия, Германия, Дания, Исландия, Люксембург, Нидерланды, Норвегия, "
    "Словакия, Словения, Финляндия, Хорватия, Чехия, Швеция\n"

    "<b>Запишись напрямую</b>\n"
    "• Франция: заявка на demarche.numerique.gouv.fr → «Demande de rendez-vous visas - Chypre»\n"
    "• Испания: письмо на emb.nicosia.sc@maec.es, тема CITA PREVIA\n"
    "• Италия: prenotami.esteri.it\n\n"
    "<b>Команды</b>\n" + "\n".join(f"/{c} — {d}" for c, d in USER_COMMANDS)
)
ADMIN_HELP = "\n\n<b>Админ</b>\n" + "\n".join(f"/{c} — {d}" for c, d in ADMIN_COMMANDS)
MAX_LEN = 3800


def is_admin(uid: int, settings: Settings) -> bool:
    return uid in settings.admin_ids


DESCRIPTION = (
    "Ловим свободные слоты на шенгенскую визу с Кипра.\n\n"
    "Выбираешь страну и даты — отслеживаем запись 24/7 и сообщаем, как только появится свободный слот. "
    "Хочешь — записываем за тебя. Оплата только после брони.\n\n"
    "🎁 Каждый 5-й клиент получает слот бесплатно.\n\n"
    "17 стран: Австрия, Бельгия, Венгрия, Германия, Греция, Дания, Исландия, Люксембург, Мальта, "
    "Нидерланды, Норвегия, Словакия, Словения, Финляндия, Хорватия, Чехия, Швеция.\n\n"
    "Нажми «Старт», чтобы начать."
)
SHORT_DESCRIPTION = "Ловим свободные слоты на шенген-визу с Кипра — сообщим, как только появится. Оплата после брони."


def price_str(settings: Settings) -> str:
    return f"{settings.default_price:.0f}"


def help_text(settings: Settings, admin: bool = False) -> str:
    extra = ""
    if not admin:
        if settings.manager_contact:
            extra += f"\n\n{settings.manager_contact} — вопросы и помощь с заявкой."
        if settings.channel:
            extra += f"\n📣 Свежие слоты и кейсы — в нашем канале {settings.channel}"
    return (HELP.replace("{price}", price_str(settings)).replace("{docs_price}", f"{settings.docs_price:.0f}")
            .replace("{family}", f"{settings.family_price:.0f}") + extra + (ADMIN_HELP if admin else ""))


FAQ = (
    "<b>Частые вопросы</b>\n\n"
    "<b>Что от меня нужно?</b>\nЗаполнить заявку в /order (2–3 минуты): страна, даты, данные паспорта. "
    "Когда запишем — оплатить наш сервис. И прийти на подачу с документами в назначенное время.\n\n"
    "<b>Как быстро найдёте слот?</b>\nЗависит от страны, дат и удачи: консульства выпускают слоты в разном количестве, "
    "а освободившиеся после отмен разбирают за минуты. Записываем сразу, как появляется подходящий слот. "
    "Гарантировать дату не может никто — слоты выпускает консульство.\n\n"
    "<b>Нас трое (семья). Сколько слотов нужно?</b>\nОдин человек — один слот, у каждого своё время приёма. "
    "Оформи заявку на каждого. Хотите в один день — напиши в /support, постараемся.\n\n"
    "<b>Нужно срочно, на этой неделе</b>\nЕсли консульство выпустит слот на эти даты — запишем. Если нет — "
    "записать невозможно. Напиши даты как есть, например «ближайшие 2 недели».\n\n"
    "<b>Насколько забирают паспорт?</b>\nОбычно на 2–3 недели после подачи. Планируй поездку с запасом. "
    "\n\n"
    "<b>Сколько стоит?</b>\nСервис — {price} € с человека. Семья из 3 (2 взрослых + ребёнок) — пакет {family} € за всех. "
    "Это только наш сервис; консульский и визовый сборы оплачиваешь сам в центре.\n\n"
    "<b>Как оплата?</b>\nЗа сервис — только после того, как запись сделана и ты получил подтверждение. "
    "Для Венгрии сбор визового центра оплачивается заранее: мы платим его при брони.\n\n"
    "<b>Какие документы собрать?</b>\nОфициальные списки пришлём после выбора страны в /order. "
    "Можем собрать весь пакет за тебя — /support."
)


TERMS = (
    "<b>Условия сервиса</b>\n\n"
    "1. <b>Что мы делаем.</b> Помогаем с записью на подачу документов: ловим свободный слот под твои даты и записываем "
    "тебя через наш кабинет визового центра. Мы не связаны с консульствами и визовыми центрами и не влияем на решение по визе.\n\n"
    "2. <b>Когда услуга оказана.</b> Когда мы прислали тебе подтверждение записи (PDF). После этого услугу нужно оплатить "
    "— счёт придёт в бот.\n\n"
    "3. <b>Изменения и отмена.</b> Передумал, поменялись даты или планы — напиши в /support как можно раньше, до брони: "
    "изменим или отменим заявку бесплатно. После брони услуга считается оказанной. Перенос записи возможен не всегда — "
    "только если его разрешит визовый центр.\n\n"
    "4. <b>Какой слот.</b> Записываем в первый подходящий слот в указанные тобой даты. Слоты выпускает консульство, "
    "поэтому гарантировать конкретную дату не может никто.\n\n"
    "5. <b>Сроки.</b> Иногда запись может прийти всего за 2–7 дней до подачи. "
    "Документы, на сбор которых нужно время, готовь заранее.\n\n"
    "6. <b>Сборы.</b> Если визовый центр берёт сбор при записи (сейчас — Венгрия), ты оплачиваешь его заранее, мы платим "
    "его при брони, квитанция будет в подтверждении записи. Если до брони отменишь заявку или мы не найдём слот — сбор вернём. "
    "Консульский и прочие сборы в день подачи оплачиваешь сам в визовом центре.\n\n"
    "7. <b>Документы.</b> Документы на визу готовишь сам по официальным спискам — ссылки пришлём. Подскажем по вопросам, "
    "если знаем ответ. Можем собрать весь пакет документов за тебя за отдельную плату — /support.\n\n"
    "8. <b>Данные.</b> Используем твои данные только для записи. Паспортные данные хранятся в зашифрованном виде."
)


@router.message(Command("terms"))
async def terms(message: Message) -> None:
    await message.answer(TERMS)


@router.message(Command("faq"))
async def faq(message: Message, settings: Settings) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Записаться на визу", callback_data="go:order")
    await message.answer(FAQ.replace("{price}", price_str(settings)).replace("{family}", f"{settings.family_price:.0f}"),
                         reply_markup=kb.as_markup())


async def setup_commands(bot, settings: Settings) -> None:
    try:
        await bot.set_my_description(DESCRIPTION.replace("{price}", price_str(settings)).replace("{family}", f"{settings.family_price:.0f}"))
        await bot.set_my_short_description(SHORT_DESCRIPTION.replace("{price}", price_str(settings)))
    except Exception:
        pass  # rate-limited by Telegram if called too often; not critical
    user = [BotCommand(command=c, description=d) for c, d in USER_COMMANDS]
    await bot.set_my_commands(user, scope=BotCommandScopeDefault())
    admin = user + [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS]
    for admin_id in settings.admin_targets:
        try:
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass  # admin has not started the bot yet


# ---- middlewares ----
class TrackingMiddleware(BaseMiddleware):
    """Outer middleware: every update refreshes the user and lands in the analytics log."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        db: Database = data["db"]
        settings: Settings = data["settings"]
        if user and not user.is_bot:
            name = html.escape(user.full_name or "")
            is_new, allowed = await db.touch_user(
                user.id, name, user.username, user.language_code,
                allow_new=settings.open_access or is_admin(user.id, settings))
            data["is_new_user"] = is_new
            if isinstance(event, Message):
                text = event.text or ""
                kind, payload = ("command", text.split()[0].split("@")[0]) if text.startswith("/") else ("message", text[:60])
            else:
                kind, payload = "callback", (event.data or "").split(":")[0]
            await db.log_event(user.id, kind, payload)
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        settings: Settings = data["settings"]
        db: Database = data["db"]
        if user is None:
            return None
        if is_admin(user.id, settings):
            return await handler(event, data)
        if await db.is_banned(user.id):
            return None
        if await db.is_allowed(user.id):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("Доступ пока не выдан, ждём подтверждения.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None


def attach_access(r: Router) -> None:
    r.message.middleware(AccessMiddleware())
    r.callback_query.middleware(AccessMiddleware())


async def send_long(message: Message, lines: list[str], **kw) -> None:
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk, **kw)


def ago(d: datetime | None, tz: ZoneInfo) -> str:
    if not d:
        return "—"
    secs = (datetime.now(d.tzinfo) - d).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)} мин назад"
    if secs < 86400:
        return f"{int(secs // 3600)} ч назад"
    return d.astimezone(tz).strftime("%d.%m.%Y")


# ---- start / help ----
@router.message(CommandStart())
async def start(message: Message, command: CommandObject, db: Database, settings: Settings, state: FSMContext,
                is_new_user: bool = False) -> None:
    u = message.from_user
    admin = is_admin(u.id, settings)
    source = (command.args or "").strip()
    action = None
    if source.startswith("ref") and source[3:].isdigit():
        if is_new_user and await db.set_referrer(u.id, int(source[3:])):
            await db.log_event(u.id, "referred", source[3:])
        source = "referral"
    else:
        from .deeplink import parse_start_payload
        action, source = parse_start_payload(source)
        if action:
            await db.log_event(u.id, "deeplink", f"{action}:{source}")
    if source:
        await db.set_source(u.id, re.sub(r"[^\w\-]", "", source))
    name = html.escape(u.full_name + (f" @{u.username}" if u.username else ""))
    allowed = admin or await db.is_allowed(u.id)

    if is_new_user and not admin:
        note = f"👤 Новый пользователь: {name} (<code>{u.id}</code>)\nИсточник: {html.escape(source) or 'прямой заход'}"
        kb = None
        if not allowed:
            b = InlineKeyboardBuilder()
            b.button(text="Разрешить", callback_data=f"allow:{u.id}")
            b.button(text="Заблокировать", callback_data=f"ban:{u.id}")
            kb = b.as_markup()
        for admin_id in settings.admin_targets:
            try:
                await message.bot.send_message(admin_id, note, reply_markup=kb)
            except Exception:
                pass

    if allowed:
        if admin:
            await setup_commands(message.bot, settings)
        if source == "case":
            from .cases import start_case
            await start_case(message, state)
            return
        if action == "wheel":
            from .promo import wheel
            await wheel(message, db)
            return
        if action == "invite":
            from .promo import invite
            await invite(message, db)
            return
        if action == "order":
            from .orders import order_start
            await order_start(message, state, db, settings)
            return
        await message.answer(help_text(settings, admin))
    else:
        await message.answer("Привет! Запрос на доступ отправлен, скоро откроем.")


@router.message(Command("help"))
async def help_(message: Message, settings: Settings) -> None:
    await message.answer(help_text(settings, is_admin(message.from_user.id, settings)))


# ---- views ----
def mission_lines(rows, tz: ZoneInfo, poller: Poller) -> list[str]:
    if not rows:
        return []
    r0 = rows[0]
    lines = [f"\n<b>{html.escape(r0['name'])}</b>"]
    checked = f"проверено {ago(r0['fetched_at'], tz)}" if r0["fetched_at"] else "ещё не проверялась"
    dated = [r for r in rows if r["category"] is not None and r["earliest"]]
    if dated:
        for r in dated:
            lines.append(f"  🟢 {html.escape(r['category'])}: <b>{fmt_date(r['earliest'])}</b>")
            lines.append(f"     {html.escape(r['centre'])}")
    else:
        lines.append("  ⚪️ слотов нет")
    lines.append(f"  {checked}" + (f" · данные от {html.escape(r0['vfs_updated'])}" if r0["vfs_updated"] else ""))
    if r0["last_error"]:
        lines.append("  ⚠️ последняя проверка с ошибкой")
    lines.append(f"  {poller.page_url(r0['code'])}")
    return lines


@router.message(Command("status"))
async def status(message: Message, db: Database, poller: Poller, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        await message.answer("Такой команды нет. Список команд — /help")
        return
    rows = await db.status()
    if not rows:
        await message.answer("Данных пока нет.")
        return
    tz = ZoneInfo(settings.tz)
    by_code: dict[str, list] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    open_first = sorted(by_code.values(), key=lambda g: not any(r["earliest"] for r in g))
    lines = ["<b>Ближайшие даты сейчас</b>"]
    for group in open_first:
        lines += mission_lines(group, tz, poller)
    await send_long(message, lines)


@router.message(Command("history"))
async def history(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        await message.answer("Такой команды нет. Список команд — /help")
        return
    code = (command.args or "").strip().lower()
    if not code:
        await message.answer("Формат: /history hun")
        return
    rows = await db.history(code)
    if not rows:
        await message.answer("Изменений пока не было.")
        return
    tz = ZoneInfo(settings.tz)
    labels = {"opened": "🟢 открылся", "earlier": "⏫ раньше", "later": "⏬ позже", "closed": "⚪️ закрылся"}
    lines = [f"<b>История {html.escape(code)}</b>"]
    for r in rows:
        when = r["at"].astimezone(tz).strftime("%d.%m %H:%M")
        lines.append(f"{when} {labels.get(r['kind'], r['kind'])} {fmt_date(r['old_date'])} → "
                     f"{fmt_date(r['new_date'])} · {html.escape(r['category'])}")
    await send_long(message, lines)


# ---- admin ----
def admin_keyboard():
    from aiogram.types import WebAppInfo
    from .web import public_url
    kb = InlineKeyboardBuilder()
    u = public_url()
    if u and u.startswith("https://"):
        kb.button(text="📊 Открыть кабинет", web_app=WebAppInfo(url=f"{u}/panel"))
    kb.button(text="🔄 Обновить", callback_data="adm:dash")
    kb.button(text="👥 Пользователи", callback_data="adm:users:0")
    kb.button(text="🩺 Проверки", callback_data="adm:health")
    kb.button(text="📈 По дням", callback_data="adm:days")
    kb.button(text="📋 Заявки", callback_data="adm:orders")
    kb.button(text="📊 Слоты", callback_data="adm:slots")
    kb.button(text="🧭 Воронка", callback_data="adm:funnel")
    kb.adjust(*([1] if public_url() else []), 2, 2, 2)
    return kb.as_markup()


async def dashboard_text(db: Database) -> str:
    u = await db.user_summary()
    o = await db.orders_summary()
    lines = [
        "<b>Панель админа</b>",
        "",
        f"👥 Пользователей: <b>{u['total']}</b> (оставили заявку: {u['with_orders']})",
        f"🆕 Новых: {u['new_1d']} за сутки · {u['new_7d']} за 7 дн · {u['new_30d']} за 30 дн",
        f"🔥 Активных: {u['active_1d']} за сутки · {u['active_7d']} за 7 дн",
        f"✉️ Обращений в поддержку: {await db.support_count(7)} за 7 дн · "
        f"запросов на сбор документов: {await db.event_total('docs_request', 30)} за 30 дн",
        f"🚫 Забанено: {u['banned']} · заблокировали бота: {u['blocked_bot']}"
        + (f" · ждут доступа: {u['pending']}" if u["pending"] else ""),
        "",
        "<b>Заявки</b>",
        f"💳 {o['awaiting_fee']} ждут сбор · 🆕 {o['new']} новых · 🔎 {o['in_progress']} в работе · "
        f"📅 {o['booked']} ждут оплаты · 💰 {o['paid']} оплачено",
        f"Выручка сервиса: {o['revenue_30d']:.0f} € за 30 дн · {o['revenue_total']:.0f} € всего",
        f"Собрано сборов (уходят VFS): {o['fees_total']:.2f} €",
        "",
        "<b>Источники</b>",
    ]
    for r in await db.top_sources():
        lines.append(f"• {html.escape(r['source'])}: {r['n']} (+{r['n_7d']} за 7 дн)")
    by_dest = await db.orders_by_destination()
    if by_dest:
        from .orders import DESTINATIONS
        lines += ["", "<b>Заявки по странам</b> (всего · открытых)"]
        for r in by_dest:
            name = DESTINATIONS.get(r["destination"], (r["destination"] or "—",))[0]
            lines.append(f"• {html.escape(name)}: {r['n']} · {r['open']}")
    cmds = [r for r in await db.event_counts(7) if r["kind"] == "command"][:6]
    if cmds:
        lines += ["", "<b>Популярные команды, 7 дн</b>", " · ".join(f"{html.escape(r['payload'])} {r['n']}" for r in cmds)]
    return "\n".join(lines)


async def users_text(db: Database, page: int, tz: ZoneInfo) -> tuple[str, object]:
    per_page = 15
    rows = await db.users_page(page * per_page, per_page + 1)
    has_next = len(rows) > per_page
    lines = [f"<b>Пользователи</b>, стр. {page + 1} (сортировка по активности)"]
    for r in rows[:per_page]:
        mark = "🚫" if r["banned"] else ("⏳" if not r["allowed"] else ("💤" if r["blocked_bot"] else "✅"))
        uname = f" @{r['username']}" if r["username"] else ""
        src = f" · {html.escape(r['source'])}" if r["source"] else ""
        lines.append(f"{mark} {r['name'] or '—'}{html.escape(uname)} · <code>{r['tg_id']}</code>\n"
                     f"     был {ago(r['last_seen'], tz)} · пришёл {r['created_at'].astimezone(tz).strftime('%d.%m')}"
                     f" · заявок {r['orders']}{src}")
    lines.append("\nКарточка: /user ID")
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="← Назад", callback_data=f"adm:users:{page - 1}")
    if has_next:
        kb.button(text="Дальше →", callback_data=f"adm:users:{page + 1}")
    kb.button(text="⬅️ Панель", callback_data="adm:dash")
    return "\n".join(lines), kb.as_markup()


async def health_text(db: Database, poller: Poller, tz: ZoneInfo) -> str:
    h = poller.health
    lines = ["<b>Проверки</b>", f"Последний цикл: {ago(h.last_cycle_at, tz)} · backoff ×{h.backoff}"
             + (" · идёт разведка" if h.discovering else "")]
    for r in await db.missions_health():
        fails = h.fails.get(r["code"], 0)
        mark = "🟢" if not fails else ("🟡" if fails < 3 else "🔴")
        free = ""
        if r["available"] and r["n_dates"]:
            free = f" · свободно дней: {r['n_dates']}"
        elif r["available"]:
            free = " · есть слот"
        lines.append(f"{mark} {html.escape(r['name'])}: успех {ago(r['fetched_at'], tz)}{free}"
                     + (f", ошибок подряд {fails}" if fails else ""))
        if fails and r["last_error"]:
            lines.append(f"     <code>{html.escape(r['last_error'][:150])}</code>")
    return "\n".join(lines)


async def admin_only(event, settings: Settings) -> bool:
    uid = event.from_user.id
    if is_admin(uid, settings):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer("Только для админа", show_alert=True)
    return False


@router.message(Command("admin"))
async def admin(message: Message, db: Database, settings: Settings) -> None:
    if await admin_only(message, settings):
        await message.answer(await dashboard_text(db), reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("adm:"))
async def admin_cb(call: CallbackQuery, db: Database, settings: Settings, poller: Poller) -> None:
    if not await admin_only(call, settings):
        return
    tz = ZoneInfo(settings.tz)
    parts = call.data.split(":")
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ Панель", callback_data="adm:dash")
    if parts[1] == "dash":
        text, kb = await dashboard_text(db), admin_keyboard()
    elif parts[1] == "users":
        text, kb = await users_text(db, int(parts[2]), tz)
    elif parts[1] == "funnel":
        from . import funnel
        text, kb = funnel.text(await funnel.build(db, 30)), back.as_markup()
    elif parts[1] == "slots":
        from .slotstats import report
        text, kb = report(await db.observations(30), 30, settings.tz, await db.first_observation()), back.as_markup()
    elif parts[1] == "orders":
        from .orders import OPEN, STATUS
        rows = await db.orders_list(OPEN, limit=40)
        kb_o = InlineKeyboardBuilder()
        lines = ["<b>Открытые заявки</b>"] if rows else ["Открытых заявок нет."]
        for r in rows:
            window = f"{fmt_date(r['date_from'])}–{fmt_date(r['date_to'])}" if r["date_from"] else "любые"
            lines.append(f"#{r['id']} {html.escape(r['mission_name'] or r['mission'])} · {window} · {STATUS[r['status']]}")
            kb_o.button(text=f"#{r['id']}", callback_data=f"oa:open:{r['id']}")
        kb_o.button(text="⬅️ Панель", callback_data="adm:dash")
        kb_o.adjust(5)
        text, kb = "\n".join(lines), kb_o.as_markup()
    elif parts[1] == "health":
        text, kb = await health_text(db, poller, tz), back.as_markup()
    else:
        rows = await db.daily_new_users(14)
        peak = max((r["n"] for r in rows), default=0) or 1
        text = "<b>Новые пользователи по дням</b>\n" + "\n".join(
            f"<code>{r['day'].strftime('%d.%m')} {'█' * round(10 * r['n'] / peak):<10} {r['n']}</code>" for r in rows)
        kb = back.as_markup()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # message not modified
    await call.answer()


@router.message(Command("panel"))
async def panel_cmd(message: Message, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    from aiogram.types import WebAppInfo
    from .web import panel_key, public_url
    url = public_url()
    if not url:
        await message.answer("Кабинет не опубликован: у сервиса на Railway нет публичного домена.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Открыть в Telegram", web_app=WebAppInfo(url=f"{url}/panel"))
    text = "Кабинет открывается кнопкой ниже прямо в Telegram."
    if panel_key():
        text += (f"\n\nДля компьютера — личная ссылка, никому не пересылай:\n<code>{url}/panel#key={panel_key()}</code>")
    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("slots"))
async def slots_cmd(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    from .slotstats import report
    arg = (command.args or "").strip()
    days = int(arg) if arg.isdigit() and 1 <= int(arg) <= 365 else 30
    await send_long(message, report(await db.observations(days), days, settings.tz, await db.first_observation()).split("\n"))


@router.message(Command("users"))
async def users(message: Message, db: Database, settings: Settings) -> None:
    if await admin_only(message, settings):
        text, kb = await users_text(db, 0, ZoneInfo(settings.tz))
        await message.answer(text, reply_markup=kb)


@router.message(Command("user"))
async def user_card(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /user 123456789")
        return
    user, subs, events = await db.user_card(int(arg))
    if not user:
        await message.answer("Не найден.")
        return
    tz = ZoneInfo(settings.tz)
    state = "забанен" if user["banned"] else ("ждёт доступа" if not user["allowed"] else "активен")
    lines = [
        f"<b>{user['name'] or '—'}</b>" + (f" @{html.escape(user['username'])}" if user["username"] else ""),
        f"ID: <code>{user['tg_id']}</code> · {state}" + (" · заблокировал бота" if user["blocked_bot"] else ""),
        f"Пришёл: {user['created_at'].astimezone(tz).strftime('%d.%m.%Y %H:%M')} · источник: {html.escape(user['source'] or 'прямой заход')}",
        f"Последняя активность: {ago(user['last_seen'], tz)} · язык: {user['lang'] or '—'}",
        "", "<b>Заявки</b>",
    ]
    lines += [f"#{o['id']} {html.escape(o['destination'] or o['mission'])} · {o['status']}" for o in subs] or ["нет"]
    lines += ["", "<b>Последние действия</b>"]
    lines += [f"{e['at'].astimezone(tz).strftime('%d.%m %H:%M')} {e['kind']} {html.escape(e['payload'] or '')}" for e in events]
    kb = InlineKeyboardBuilder()
    if user["banned"]:
        kb.button(text="Разблокировать", callback_data=f"unban:{user['tg_id']}")
    else:
        kb.button(text="Заблокировать", callback_data=f"ban:{user['tg_id']}")
    if not user["allowed"]:
        kb.button(text="Разрешить", callback_data=f"allow:{user['tg_id']}")
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.message(Command("link"))
async def link(message: Message, command: CommandObject, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    tag = re.sub(r"[^\w\-]", "", (command.args or "").strip())[:64]
    if not tag:
        await message.answer("Формат: /link community — получишь ссылку, по которой видно, откуда пришёл человек.")
        return
    me = await message.bot.get_me()
    await message.answer(f"https://t.me/{me.username}?start={tag}\nПереходы с меткой «{tag}» будут в /admin → Источники.")


@router.message(Command("broadcast"))
async def broadcast(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /broadcast текст сообщения")
        return
    targets = await db.broadcast_targets()
    await message.answer(f"Отправляю {len(targets)} пользователям…")

    async def job():
        from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
        ok = 0
        for uid in targets:
            for _ in range(3):
                try:
                    await message.bot.send_message(uid, text)
                    ok += 1
                    break
                except TelegramRetryAfter as e:   # flood control — wait and retry, don't flag the user
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramForbiddenError:    # only a real block/deactivation flags the user
                    await db.mark_blocked_bot(uid)
                    break
                except Exception:
                    log.warning("broadcast to %s failed", uid)
                    break
            await asyncio.sleep(0.05)
        await message.answer(f"Рассылка завершена: доставлено {ok} из {len(targets)}.")

    asyncio.create_task(job())


@router.message(Command("post"))
async def post_to_channel(message: Message, command: CommandObject, settings: Settings) -> None:
    """Post to the public channel. Either /post <text>, or reply to any message with /post to repost it."""
    if not await admin_only(message, settings):
        return
    if not settings.channel:
        await message.answer("Канал не настроен. Добавь CHANNEL=@bestvisaeurope в .env и перезапусти бота.")
        return
    try:
        if message.reply_to_message:
            await message.bot.copy_message(settings.channel, message.chat.id, message.reply_to_message.message_id)
        else:
            text = (command.args or "").strip()
            if not text:
                await message.answer("Формат: /post текст — или ответь командой /post на сообщение, чтобы запостить его в канал.")
                return
            await message.bot.send_message(settings.channel, text)
        await message.answer(f"✅ Опубликовано в {settings.channel}")
    except Exception as e:
        await message.answer(f"Не вышло запостить: {html.escape(str(e))[:200]}\n"
                             "Проверь, что бот — админ канала и CHANNEL указан верно.")


async def _set_flag(bot, db: Database, uid: int, action: str) -> str:
    if action == "allow":
        name = await db.set_allowed(uid, True)
        if name is not None:
            try:
                await bot.send_message(uid, "Доступ выдан! Команды — /help")
            except Exception:
                pass
    else:
        name = await db.set_banned(uid, action == "ban")
    if name is None:
        return "Пользователь не найден, пусть сначала нажмёт /start."
    verbs = {"allow": "Разрешён доступ", "ban": "Заблокирован", "unban": "Разблокирован"}
    return f"{verbs[action]}: {name}"


@router.message(Command("allow", "ban", "unban", "deny"))
async def flag_cmd(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    if not (command.args or "").strip().isdigit():
        await message.answer(f"Формат: /{command.command} 123456789")
        return
    action = "ban" if command.command == "deny" else command.command
    await message.answer(await _set_flag(message.bot, db, int(command.args.strip()), action))


@router.callback_query(F.data.regexp(r"^(allow|ban|unban):\d+$"))
async def flag_cb(call: CallbackQuery, db: Database, settings: Settings) -> None:
    if not await admin_only(call, settings):
        return
    action, uid = call.data.split(":")
    result = await _set_flag(call.bot, db, int(uid), action)
    try:
        await call.message.edit_text(call.message.html_text + f"\n\n{result}")
    except Exception:
        pass
    await call.answer(result)


@router.message(Command("health"))
async def health(message: Message, db: Database, poller: Poller, settings: Settings) -> None:
    if await admin_only(message, settings):
        await message.answer(await health_text(db, poller, ZoneInfo(settings.tz)))


@router.message(Command("backup"))
async def backup_cmd(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if not await admin_only(message, settings):
        return
    await message.answer("🗄 Делаю бэкап БД…")
    try:
        from .backup import run_backup
        n = await run_backup(db, bot, settings)
        await message.answer(f"✅ Бэкап отправлен ({max(1, n // 1024)} КБ).")
    except Exception as e:
        await message.answer(f"⚠️ Бэкап не удался: <code>{html.escape(str(e)[:200])}</code>")


@router.message(Command("vfslogin"))
async def vfslogin(message: Message, command: CommandObject, poller: Poller, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    from . import capsolver, mailbox, vfs_auth, vfs_browser
    if not vfs_auth.accounts():
        await message.answer("Нет VFS_ACCOUNTS в переменных.")
        return
    mission = (command.args or "aut").strip().lower()
    if capsolver.enabled():
        bal = await capsolver.balance()
        sk = await capsolver.sitekey("cyp", mission, poller.s.proxy_url)
        method = (f"Capsolver (баланс ${bal if bal is not None else '?'}, "
                  f"sitekey {'найден' if sk else 'НЕ найден'})")
    else:
        method = f"Camoufox (браузер {'готов' if vfs_browser.available() else 'НЕ установлен'})"
    await message.answer(f"Логинюсь в VFS ({mission}) — {method}… до минуты.")

    async def wait(**kw):
        return await mailbox.wait_for(**kw) if mailbox.enabled() else None
    try:
        sess = await vfs_auth.live_session("cyp", mission, poller.s.proxy_url, wait)
        from .vfs_browser import STEPS
        trace = "\n".join(f"• {x}" for x in STEPS) if STEPS else "нет шагов"
        if sess and sess.alive:
            await message.answer("✅ Вход прошёл! Токен получен — автобронь VFS возможна.\n\n" + trace)
        else:
            await message.answer("⚠️ Токен не получен. Шаги:\n" + trace)
    except Exception as e:
        await message.answer(f"⚠️ Вход не прошёл: <code>{html.escape(str(e))[:400]}</code>")


@router.message(Command("capsolver"))
async def capsolver_diag(message: Message, command: CommandObject, poller: Poller, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    from . import capsolver
    mission = (command.args or "aut").strip().lower()
    await message.answer("Проверяю Capsolver…")
    try:
        report = await capsolver.diagnose("cyp", mission, poller.s.proxy_url)
    except Exception as e:
        report = f"ошибка диагностики: {html.escape(str(e))[:300]}"
    await message.answer(f"<b>Capsolver ({mission})</b>\n{report}")


@router.message(Command("slotprobe"))
async def slotprobe(message: Message, command: CommandObject, poller: Poller, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    mission = (command.args or "hun").strip().lower()
    await message.answer(f"Логинюсь в VFS ({mission}) и смотрю, что отдают эндпоинты записи из аккаунта…")
    from . import mailbox, vfs_auth
    from .slot_probe import probe_account

    async def wait(**kw):
        return await mailbox.wait_for(**kw) if mailbox.enabled() else None
    try:
        sess = await vfs_auth.live_session("cyp", mission, poller.s.proxy_url, wait)
        if not (sess and sess.alive):
            await message.answer("Не вошёл — сначала нужен успешный /vfslogin.")
            return
        await message.answer(await probe_account(sess))
    except Exception as e:
        await message.answer(f"Ошибка: <code>{html.escape(str(e))[:400]}</code>")


@router.message(Command("monprobe"))
async def monprobe(message: Message, command: CommandObject, poller: Poller, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    mission = (command.args or "aut").strip().lower()
    await message.answer(f"Публичный поиск слотов ({mission}), сырой ответ…")
    try:
        raw = await poller.client.raw_probe(mission)
        await message.answer(f"<code>{html.escape(raw[:3000])}</code>")
    except Exception as e:
        await message.answer(f"Ошибка: <code>{html.escape(str(e))[:400]}</code>")
    # Run the REAL production parser so we confirm the whole chain, not just HTTP 200.
    try:
        slots, updated = await poller.client.fetch(mission)
        dated = [s for s in slots if s.earliest]
        unparsed = [s.raw_date for s in slots if s.raw_date and not s.earliest]
        lines = [f"🔎 Разобрано: {len(slots)} центр(ов), с датами {len(dated)}, данные от {updated or '—'}"]
        for s in dated[:5]:
            lines.append(f"• {html.escape(s.centre)} / {html.escape(s.category)}: {s.earliest.isoformat()}")
        if unparsed:
            lines.append("⚠️ НЕ разобрал даты: " + ", ".join(f"<code>{html.escape(str(u)[:30])}</code>" for u in unparsed[:3]))
        if not dated and not unparsed:
            lines.append("Свободных дат сейчас нет — это норма (парсер отработал чисто).")
        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"Парсинг/запрос вернул: <code>{html.escape(str(e))[:300]}</code> "
                             "(NotOffered/Blocked здесь — это норма, если VFS временно пустой)")


@router.message(Command("calprobe"))
async def calprobe(message: Message, command: CommandObject, poller: Poller, settings: Settings) -> None:
    """Decisive real-time test: browser-login, then call the availability endpoints from inside the
    browser session (which passed Cloudflare). 200 here = real-time monitoring is viable."""
    if not await admin_only(message, settings):
        return
    mission = (command.args or "aut").strip().lower()
    from . import vfs_auth, vfs_browser
    if not vfs_browser.available():
        await message.answer("Браузер (Camoufox) недоступен в этой сборке.")
        return
    accs = vfs_auth.accounts()
    if not accs:
        await message.answer("Нет VFS-аккаунтов (VFS_ACCOUNTS в .env).")
        return
    email, pwd = accs[0]
    await message.answer(f"🧪 Браузерный логин в VFS ({mission}) + реалтайм-календарь изнутри сессии… (до 1–2 мин)")
    try:
        data = await vfs_browser.login("cyp", mission, email, pwd, proxy=settings.proxy_url, calendar_probe=True)
    except Exception as e:
        await message.answer(f"Ошибка: <code>{html.escape(str(e))[:300]}</code>")
        return
    if not data:
        await message.answer("Логин не прошёл (нет токена). Детали — в логах (last_login.png/html).")
        return
    probe = data.get("calendar_probe") or {}
    lines = ["✅ Логин прошёл. Реалтайм-эндпоинты VFS изнутри браузера:"]
    for path, res in probe.items():
        name = path.split("/")[-1]
        if "error" in res:
            lines.append(f"• {name}: ERROR {html.escape(str(res['error'])[:150])}")
        else:
            body = html.escape(str(res.get("body", ""))[:700])
            lines.append(f"• {name}: <b>{res.get('status')}</b>\n<code>{body}</code>")
    await message.answer("\n".join(lines)[:4000])


@router.message(Command("rkform"))
async def rkform(message: Message, poller: Poller, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    await message.answer("Иду к форме записи Германии (ничего не отправляю)…")
    from .rk_book import inspect_form
    try:
        await message.answer(await inspect_form(poller.rk))
    except Exception as e:
        await message.answer(f"Ошибка: <code>{html.escape(str(e))[:300]}</code>")


@router.message(Command("discover"))
async def discover(message: Message, poller: Poller, settings: Settings) -> None:
    if not await admin_only(message, settings):
        return
    if poller.health.discovering:
        await message.answer("Разведка уже идёт.")
        return
    await message.answer("Запускаю разведку по всем странам VFS, займёт пару минут.")

    async def job():
        await message.answer(await poller.discover())
        poller.wake()

    asyncio.create_task(job())


LOOKS_LIKE_FORM = re.compile(r"\d{1,2}[./]\d{1,2}|@|^\+?\d{8,}|^[A-Za-z]+ [A-Za-z]+$")


@router.message()
async def fallback(message: Message) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Такой команды нет. Список команд — /help")
    elif LOOKS_LIKE_FORM.search(text):
        await message.answer("Похоже, ты заполнял заявку, но анкета закрылась (например, истекла или бот обновлялся). "
                             "Начни заново — /order, это займёт минуту.")
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Записаться на визу", callback_data="go:order")
        kb.button(text="📋 Мои заявки", callback_data="go:myorders")
        kb.button(text="📂 Собрать документы за меня", callback_data="docs:req")
        kb.button(text="🎁 Скидки и друзья", callback_data="go:promo")
        kb.button(text="❓ Частые вопросы", callback_data="go:faq")
        kb.button(text="✉️ Поддержка", callback_data="go:support")
        kb.adjust(1)
        await message.answer("Я помогаю записаться на шенгенскую визу с Кипра. Что сделать?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("go:"))
async def go(call: CallbackQuery, db: Database, poller: Poller, settings: Settings, state: FSMContext) -> None:
    await call.answer()
    target = call.data.split(":")[1]
    if target == "order":
        from .orders import order_start
        await order_start(call.message, state, db, settings, uid=call.from_user.id)
    elif target == "faq":
        await call.message.answer(FAQ.replace("{price}", price_str(settings)).replace("{family}", f"{settings.family_price:.0f}"))
    elif target == "promo":
        from .promo import referral_link
        link = await referral_link(call.bot, call.from_user.id)
        invited, paid, _ = await db.referral_stats(call.from_user.id)
        await call.message.answer(
            f"🤝 <b>Приведи друга</b> — когда он оплатит запись, тебе −50% на следующую заявку.\n\n"
            f"Твоя ссылка:\n{link}\n\nПриглашено: {invited}, оплатили: {paid}\n\n"
            "🎡 Ещё есть колесо фортуны со скидкой: /wheel", disable_web_page_preview=True)
    elif target == "support":
        from .support import ask_question
        await ask_question(call.message, state)
    elif target == "myorders":
        from .orders import my_orders
        await my_orders(call.message, db, uid=call.from_user.id)


@router.callback_query()
async def stale_button(call: CallbackQuery) -> None:
    await call.answer("Эта кнопка устарела. Команды — /help", show_alert=True)

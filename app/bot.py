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
    ("order", "Заявка: забронируем слот за тебя"),
    ("myorders", "Мои заявки и оплата"),
    ("sub", "Выбрать страны для уведомлений"),
    ("status", "Ближайшие даты сейчас"),
    ("mysubs", "Мои подписки"),
    ("window", "Окно дат: /window hun 01.11.2026-30.11.2026"),
    ("history", "История по стране: /history hun"),
    ("help", "Что умеет бот"),
]
ADMIN_COMMANDS = [
    ("admin", "Панель админа"),
    ("orders", "Открытые заявки"),
    ("price", "Цены: /price hun 120"),
    ("users", "Пользователи"),
    ("user", "Карточка: /user ID"),
    ("link", "Ссылка с меткой: /link community"),
    ("broadcast", "Рассылка: /broadcast текст"),
    ("ban", "Заблокировать: /ban ID"),
    ("unban", "Разблокировать: /unban ID"),
    ("health", "Состояние проверок"),
    ("discover", "Переразведка стран VFS"),
]

HELP = (
    "Я слежу за слотами на шенгенские визы с Кипра и пишу, когда появляется дата.\n\n"
    "<b>Что отслеживается</b>\n"
    "• VFS Global, Никосия: Австрия, Венгрия, Словакия, Финляндия, Хорватия\n"
    "• Посольство Германии: Германия, Нидерланды, Бельгия, Дания, Норвегия, Исландия, Люксембург\n"
    "• Посольство Греции: Греция, Мальта (скоро)\n\n"
    "<b>Без календаря — записывайся напрямую</b>\n"
    "• Франция: заявка на demarche.numerique.gouv.fr → «Demande de rendez-vous visas - Chypre»\n"
    "• Испания: письмо на emb.nicosia.sc@maec.es, тема CITA PREVIA\n"
    "• Италия: prenotami.esteri.it (мониторинг в работе)\n\n"
    "<b>Запись под ключ</b>\n"
    "/order — оставь заявку, мы поймаем и забронируем слот на твои данные. Оплата в крипте — только после брони.\n\n"
    "<b>Команды</b>\n" + "\n".join(f"/{c} — {d}" for c, d in USER_COMMANDS) + "\n\n"
    "Данные VFS могут отставать до часа: увидел алерт — сразу иди записываться."
)
ADMIN_HELP = "\n\n<b>Админ</b>\n" + "\n".join(f"/{c} — {d}" for c, d in ADMIN_COMMANDS)
MAX_LEN = 3800


def is_admin(uid: int, settings: Settings) -> bool:
    return uid in settings.admin_ids


DESCRIPTION = (
    "Помогаю получить запись на шенгенскую визу с Кипра.\n\n"
    "🔔 Слежу за свободными датами в 12 странах: Австрия, Венгрия, Словакия, Финляндия, Хорватия, "
    "Германия, Нидерланды, Бельгия, Дания, Норвегия, Исландия, Люксембург — и пишу, как только появится слот.\n\n"
    "📝 Могу забронировать запись за тебя. Оплата — только после брони.\n\n"
    "Нажми «Старт», чтобы начать."
)
SHORT_DESCRIPTION = "Слоты на шенгенские визы с Кипра: уведомления о свободных датах и запись под ключ."


async def setup_commands(bot, settings: Settings) -> None:
    try:
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    except Exception:
        pass  # rate-limited by Telegram if called too often; not critical
    user = [BotCommand(command=c, description=d) for c, d in USER_COMMANDS]
    await bot.set_my_commands(user, scope=BotCommandScopeDefault())
    admin = user + [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS]
    for admin_id in settings.admin_ids:
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
            await event.answer("Доступ пока не выдан, жду подтверждения админа.")
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
async def start(message: Message, command: CommandObject, db: Database, settings: Settings,
                is_new_user: bool = False) -> None:
    u = message.from_user
    admin = is_admin(u.id, settings)
    source = (command.args or "").strip()
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
        for admin_id in settings.admin_ids:
            try:
                await message.bot.send_message(admin_id, note, reply_markup=kb)
            except Exception:
                pass

    if allowed:
        if admin:
            await setup_commands(message.bot, settings)
        await message.answer(HELP + (ADMIN_HELP if admin else ""))
    else:
        await message.answer("Привет! Запрос на доступ отправлен админу.")


@router.message(Command("help"))
async def help_(message: Message, settings: Settings) -> None:
    await message.answer(HELP + (ADMIN_HELP if is_admin(message.from_user.id, settings) else ""))


# ---- subscriptions ----
async def sub_keyboard(db: Database, uid: int):
    subs = {r["mission"] for r in await db.user_subs(uid)}
    kb = InlineKeyboardBuilder()
    for code, name in await db.offered_missions():
        kb.button(text=("✅ " if code in subs else "") + name, callback_data=f"sub:{code}")
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("sub"))
async def sub(message: Message, db: Database) -> None:
    if not await db.offered_missions():
        await message.answer("Список стран ещё не собран, бот проводит разведку. Попробуй через пару минут.")
        return
    await message.answer("Выбери страны для уведомлений:", reply_markup=await sub_keyboard(db, message.from_user.id))


@router.callback_query(F.data.startswith("sub:"))
async def sub_toggle(call: CallbackQuery, db: Database, poller: Poller, settings: Settings) -> None:
    code = call.data.split(":", 1)[1]
    on = await db.toggle_sub(call.from_user.id, code)
    name = await db.mission_name(code) or code
    await call.message.edit_reply_markup(reply_markup=await sub_keyboard(db, call.from_user.id))
    await call.answer(f"{name}: {'подписка включена' if on else 'подписка выключена'}")
    await db.log_event(call.from_user.id, "sub_on" if on else "sub_off", code)
    if on:
        rows = [r for r in await db.status() if r["code"] == code]
        lines = ["✅ Подписка включена. Сейчас:"] + mission_lines(rows, ZoneInfo(settings.tz), poller)
        lines.append("\nНапишу, как только появится слот или дата станет раньше.")
        await call.message.answer("\n".join(lines))


@router.message(Command("mysubs"))
async def mysubs(message: Message, db: Database) -> None:
    rows = await db.user_subs(message.from_user.id)
    if not rows:
        await message.answer("Подписок нет. Выбери страны через /sub.")
        return
    lines = ["<b>Мои подписки</b>"]
    for r in rows:
        name = await db.mission_name(r["mission"]) or r["mission"]
        window = f" · окно {fmt_date(r['date_from'])}–{fmt_date(r['date_to'])}" if r["date_from"] else ""
        lines.append(f"• {html.escape(name)} (<code>{r['mission']}</code>){window}")
    await message.answer("\n".join(lines))


WINDOW_RE = re.compile(r"^(\w{3})\s+(?:(off)|(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4}))$", re.I)


@router.message(Command("window"))
async def window(message: Message, command: CommandObject, db: Database) -> None:
    m = WINDOW_RE.match((command.args or "").strip())
    if not m:
        await message.answer("Формат: /window hun 01.11.2026-30.11.2026 или /window hun off\nКоды стран — в /mysubs.")
        return
    code = m.group(1).lower()
    if m.group(2):
        d_from = d_to = None
    else:
        try:
            d_from = datetime.strptime(m.group(3), "%d.%m.%Y").date()
            d_to = datetime.strptime(m.group(4), "%d.%m.%Y").date()
        except ValueError:
            await message.answer("Неверная дата.")
            return
        if d_from > d_to:
            d_from, d_to = d_to, d_from
    if not await db.set_window(message.from_user.id, code, d_from, d_to):
        await message.answer(f"Сначала подпишись на {code} через /sub.")
        return
    await message.answer("Окно снято." if d_from is None else f"Окно {code}: {fmt_date(d_from)} – {fmt_date(d_to)}")


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
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="adm:dash")
    kb.button(text="👥 Пользователи", callback_data="adm:users:0")
    kb.button(text="🩺 Проверки", callback_data="adm:health")
    kb.button(text="📈 По дням", callback_data="adm:days")
    kb.button(text="📋 Заявки", callback_data="adm:orders")
    kb.adjust(2)
    return kb.as_markup()


async def dashboard_text(db: Database) -> str:
    u = await db.user_summary()
    o = await db.orders_summary()
    lines = [
        "<b>Панель админа</b>",
        "",
        f"👥 Пользователей: <b>{u['total']}</b> (с подписками: {u['with_subs']})",
        f"🆕 Новых: {u['new_1d']} за сутки · {u['new_7d']} за 7 дн · {u['new_30d']} за 30 дн",
        f"🔥 Активных: {u['active_1d']} за сутки · {u['active_7d']} за 7 дн",
        f"🚫 Забанено: {u['banned']} · заблокировали бота: {u['blocked_bot']}"
        + (f" · ждут доступа: {u['pending']}" if u["pending"] else ""),
        f"🔔 Алертов отправлено: {await db.alerts_sent(1)} за сутки · {await db.alerts_sent(7)} за 7 дн",
        "",
        "<b>Заявки</b>",
        f"🆕 {o['new']} новых · 🔎 {o['in_progress']} в работе · 📅 {o['booked']} ждут оплаты · 💰 {o['paid']} оплачено",
        f"Выручка: {o['revenue_30d']:.0f} € за 30 дн · {o['revenue_total']:.0f} € всего",
        "",
        "<b>Источники</b>",
    ]
    for r in await db.top_sources():
        lines.append(f"• {html.escape(r['source'])}: {r['n']} (+{r['n_7d']} за 7 дн)")
    lines += ["", "<b>Подписки по странам</b>"]
    for r in await db.subs_by_mission():
        lines.append(f"• {html.escape(r['name'])}: {r['n']}")
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
                     f" · подписок {r['subs']}{src}")
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
        lines.append(f"{mark} {html.escape(r['name'])}: успех {ago(r['fetched_at'], tz)}"
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
        "", "<b>Подписки</b>",
    ]
    lines += [f"• {html.escape(s['name'])}" + (f" ({fmt_date(s['date_from'])}–{fmt_date(s['date_to'])})" if s["date_from"] else "")
              for s in subs] or ["нет"]
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
        ok = 0
        for uid in targets:
            try:
                await message.bot.send_message(uid, text)
                ok += 1
            except Exception:
                await db.mark_blocked_bot(uid)
            await asyncio.sleep(0.05)
        await message.answer(f"Рассылка завершена: доставлено {ok} из {len(targets)}.")

    asyncio.create_task(job())


async def _set_flag(bot, db: Database, uid: int, action: str) -> str:
    if action == "allow":
        name = await db.set_allowed(uid, True)
        if name is not None:
            try:
                await bot.send_message(uid, "Доступ выдан!\n\n" + HELP)
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
        kb.button(text="📝 Оставить заявку", callback_data="go:order")
        kb.button(text="🔔 Подписаться на слоты", callback_data="go:sub")
        kb.button(text="📅 Даты сейчас", callback_data="go:status")
        kb.adjust(1)
        await message.answer("Я бот для записи на шенгенские визы с Кипра. Что сделать?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("go:"))
async def go(call: CallbackQuery, db: Database, poller: Poller, settings: Settings, state: FSMContext) -> None:
    await call.answer()
    target = call.data.split(":")[1]
    if target == "order":
        from .orders import order_start
        await order_start(call.message, state, db, settings, uid=call.from_user.id)
    elif target == "sub":
        await call.message.answer("Выбери страны для уведомлений:", reply_markup=await sub_keyboard(db, call.from_user.id))
    else:
        await status(call.message, db, poller, settings)


@router.callback_query()
async def stale_button(call: CallbackQuery) -> None:
    await call.answer("Эта кнопка устарела. Команды — /help", show_alert=True)

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .db import Database
from .poller import Poller, fmt_date

router = Router()

HELP = (
    "Я слежу за слотами на шенгенские визы с Кипра и пишу, когда появляется дата.\n\n"
    "<b>Что отслеживается</b>\n"
    "• VFS Global, Никосия: Австрия, Венгрия, Словакия, Финляндия, Хорватия\n"
    "• Посольство Германии: Германия, Нидерланды, Бельгия, Дания, Норвегия, Исландия, Люксембург\n\n"
    "<b>Пока не отслеживается</b>\n"
    "• Франция — запись через заявку на сайте посольства, календаря нет\n"
    "• Италия — Prenotami, нужен личный кабинет (в работе)\n"
    "• Испания, Греция, Мальта, Польша и др. — проверяем их системы\n\n"
    "<b>Команды</b>\n"
    "/sub — выбрать страны для уведомлений\n"
    "/window hun 01.11.2026-30.11.2026 — уведомлять только о датах в окне\n"
    "/window hun off — снять окно\n"
    "/status — ближайшие даты сейчас\n"
    "/history hun — последние изменения по стране\n"
    "/stats — в какие часы появлялись слоты за 14 дней\n\n"
    "Данные VFS могут отставать до часа: увидел алерт — сразу иди записываться."
)
ADMIN_HELP = "\n\nАдмин: /health, /discover, /users, /allow ID, /deny ID"
MAX_LEN = 3800


def is_admin(uid: int, settings: Settings) -> bool:
    return uid in settings.admin_ids


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        settings: Settings = data["settings"]
        db: Database = data["db"]
        if user is None:
            return None
        if is_admin(user.id, settings) or await db.is_allowed(user.id):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("Доступ пока не выдан, жду подтверждения админа.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None


router.message.middleware(AccessMiddleware())
router.callback_query.middleware(AccessMiddleware())


async def send_long(message: Message, lines: list[str]) -> None:
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


@router.message(CommandStart())
async def start(message: Message, db: Database, settings: Settings) -> None:
    u = message.from_user
    name = html.escape(u.full_name + (f" @{u.username}" if u.username else ""))
    admin = is_admin(u.id, settings)
    allowed = await db.upsert_user(u.id, name, True if admin else None)
    if allowed:
        await message.answer(HELP + (ADMIN_HELP if admin else ""))
        return
    await message.answer("Привет! Запрос на доступ отправлен админу.")
    kb = InlineKeyboardBuilder()
    kb.button(text="Разрешить", callback_data=f"allow:{u.id}")
    for admin_id in settings.admin_ids:
        await message.bot.send_message(
            admin_id, f"Запрос доступа: {name} (<code>{u.id}</code>)", reply_markup=kb.as_markup())


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
        await message.answer("Список стран ещё не собран, бот проводит разведку. Попробуй позже.")
        return
    await message.answer("Выбери страны для уведомлений:", reply_markup=await sub_keyboard(db, message.from_user.id))


@router.callback_query(F.data.startswith("sub:"))
async def sub_toggle(call: CallbackQuery, db: Database, poller: Poller, settings: Settings) -> None:
    code = call.data.split(":", 1)[1]
    on = await db.toggle_sub(call.from_user.id, code)
    name = await db.mission_name(code) or code
    await call.message.edit_reply_markup(reply_markup=await sub_keyboard(db, call.from_user.id))
    await call.answer(f"{name}: {'подписка включена' if on else 'подписка выключена'}")
    if on:
        rows = [r for r in await db.status() if r["code"] == code]
        lines = ["✅ Подписка включена. Сейчас:"] + mission_lines(rows, ZoneInfo(settings.tz), poller)
        lines.append("\nНапишу, как только появится слот или дата станет раньше.")
        await call.message.answer("\n".join(lines))


WINDOW_RE = re.compile(r"^(\w{3})\s+(?:(off)|(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4}))$", re.I)


@router.message(Command("window"))
async def window(message: Message, command: CommandObject, db: Database) -> None:
    m = WINDOW_RE.match((command.args or "").strip())
    if not m:
        await message.answer("Формат: /window hun 01.11.2026-30.11.2026 или /window hun off")
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
    lines, current = [], None
    for r in rows:
        if r["code"] != current:
            current = r["code"]
            lines.append(f"\n<b>{html.escape(r['name'])}</b>")
        if r["category"] is None:
            lines.append("  ещё не опрашивалась")
            continue
        mark = "🟢" if r["earliest"] else "⚪️"
        when = fmt_date(r["earliest"]) if r["earliest"] else "слотов нет"
        lines.append(f"  {mark} {html.escape(r['category'])}: <b>{when}</b>")
        lines.append(f"     {html.escape(r['centre'])} · проверено {r['seen_at'].astimezone(tz).strftime('%H:%M')}"
                     f" · данные от {html.escape(r['vfs_updated'] or '—')}")
    if current:
        lines.append(f"  {poller.page_url(current)}")
    return lines


@router.message(Command("status"))
async def status(message: Message, db: Database, poller: Poller, settings: Settings) -> None:
    rows = await db.status()
    if not rows:
        await message.answer("Данных пока нет.")
        return
    tz = ZoneInfo(settings.tz)
    lines = ["<b>Ближайшие даты сейчас</b>"]
    by_code: dict[str, list] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    for group in by_code.values():
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
    lines = [f"<b>История {code}</b>"]
    for r in rows:
        when = r["at"].astimezone(tz).strftime("%d.%m %H:%M")
        lines.append(f"{when} {labels.get(r['kind'], r['kind'])} {fmt_date(r['old_date'])} → "
                     f"{fmt_date(r['new_date'])} · {html.escape(r['category'])}")
    await send_long(message, lines)


@router.message(Command("stats"))
async def stats(message: Message, db: Database, settings: Settings) -> None:
    rows = await db.stats(14, settings.tz)
    if not rows:
        await message.answer("За 14 дней слоты не появлялись (или бот работает меньше суток).")
        return
    by_mission: dict[str, list[str]] = {}
    for r in rows:
        by_mission.setdefault(r["mission"], []).append(f"{r['hour']:02d}:00 ×{r['n']}")
    lines = ["<b>Когда появлялись слоты, 14 дней</b> (время Кипра)"]
    for code, hours in by_mission.items():
        lines.append(f"{code}: " + ", ".join(hours[:5]))
    await send_long(message, lines)


# ---- admin ----
@router.message(Command("health"))
async def health(message: Message, poller: Poller, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    h = poller.health
    tz = ZoneInfo(settings.tz)
    t = lambda d: d.astimezone(tz).strftime("%d.%m %H:%M:%S") if d else "—"
    await message.answer(
        f"Последний цикл: {t(h.last_cycle_at)}\nПоследний успех: {t(h.last_ok_at)}\n"
        f"Провалов подряд: {h.failures}\nBackoff: ×{h.backoff}\nРазведка идёт: {'да' if h.discovering else 'нет'}\n"
        f"TLS-профиль: {poller.client.working_profile or '—'}\nПрокси: {'да' if settings.proxy_url else 'нет'}\n"
        f"Интервал: {settings.poll_interval} c\nОшибка: <code>{html.escape(h.last_error or '—')}</code>")


@router.message(Command("discover"))
async def discover(message: Message, poller: Poller, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    if poller.health.discovering:
        await message.answer("Разведка уже идёт.")
        return
    await message.answer("Запускаю разведку по всем странам Шенгена, займёт пару минут.")

    async def job():
        result = await poller.discover()
        await message.answer(result)
        poller.wake()

    asyncio.create_task(job())


@router.message(Command("users"))
async def users(message: Message, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    rows = await db.users()
    lines = [f"{'✅' if r['allowed'] else '⏳'} <code>{r['tg_id']}</code> {r['name'] or ''}" for r in rows]
    await send_long(message, lines or ["Пусто"])


async def _set_access(message_or_call, db: Database, uid: int, allowed: bool) -> str:
    name = await db.set_allowed(uid, allowed)
    if name is None:
        return "Пользователь не найден, пусть сначала нажмёт /start."
    if allowed:
        await message_or_call.bot.send_message(uid, "Доступ выдан!\n\n" + HELP)
    return f"{'Разрешён' if allowed else 'Закрыт'} доступ: {name}"


@router.message(Command("allow", "deny"))
async def allow_cmd(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    if not (command.args or "").strip().isdigit():
        await message.answer(f"Формат: /{command.command} 123456789")
        return
    await message.answer(await _set_access(message, db, int(command.args.strip()), command.command == "allow"))


@router.callback_query(F.data.startswith("allow:"))
async def allow_cb(call: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(call.from_user.id, settings):
        await call.answer("Только для админа", show_alert=True)
        return
    result = await _set_access(call, db, int(call.data.split(":", 1)[1]), True)
    await call.message.edit_text(call.message.html_text + f"\n\n{result}")
    await call.answer()

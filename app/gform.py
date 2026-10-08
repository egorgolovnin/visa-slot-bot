"""Google Form as an alternative to the in-bot form.

The bot hands each client a personal prefilled link (hidden code -> Telegram user). A Google Apps Script trigger
posts every submission to /api/form; we validate it with the same parsers as the bot form and create the order.
Env: FORM_PREFILL_URL (with the literal TOKEN placeholder), FORM_SECRET.
"""
from __future__ import annotations

import html
import logging
import os
import re
import secrets
from datetime import date

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

log = logging.getLogger("gform")
TITLES = {
    "country": "Страна поездки",
    "dates": "Даты, когда можешь подать документы",
    "count": "Сколько человек подаётся",
    "full_name": "Имя и фамилия латиницей, как в загранпаспорте",
    "gender": "Пол",
    "birth": "Дата рождения",
    "nationality": "Гражданство",
    "passport": "Номер загранпаспорта",
    "passport_exp": "Паспорт действует до",
    "residence_exp": "ВНЖ Кипра действует до",
    "phone": "Телефон (нужен визовому центру)",
    "others": "Данные остальных людей, если подаётесь вместе",
    "comment": "Комментарий",
    "token": "Код Telegram (не меняйте)",
}


def enabled() -> bool:
    return bool(os.getenv("FORM_PREFILL_URL") and os.getenv("FORM_SECRET"))


async def link_for(db, tg_id: int) -> str:
    token = secrets.token_urlsafe(9)
    await db.kv_set(f"form:{token}", str(tg_id))
    return os.environ["FORM_PREFILL_URL"].replace("TOKEN", token)


def _by_title(answers: dict) -> dict:
    out = {}
    for key, title in TITLES.items():
        for k, v in answers.items():
            if k.strip().lower().startswith(title.lower()[:25]):
                out[key] = v if not isinstance(v, list) else ", ".join(map(str, v))
    return out


def validate(a: dict) -> tuple[dict, list[str]]:
    """Normalize answers into the bot's order data; return (data, errors)."""
    from .orders import DESTINATIONS
    from .parsing import normalize_country, parse_date, parse_range
    errors, d = [], {}
    names = {v[0].lower(): k for k, v in DESTINATIONS.items() if not k.startswith("x_")}
    key = names.get((a.get("country") or "").strip().lower())
    if not key:
        errors.append("страна поездки не распознана")
    else:
        d["destination"], d["mission"] = key, DESTINATIONS[key][1]
        d["mission_name"], d["note"] = DESTINATIONS[key][0], DESTINATIONS[key][2]
    kind, d1, d2 = parse_range(a.get("dates") or "любые")
    if kind == "error":
        errors.append("даты не распознаны")
    d["date_from"], d["date_to"] = (d1.isoformat() if d1 else None), (d2.isoformat() if d2 else None)
    # the Google Form collects full data for one person only; capture the stated count as a hint for the admin
    try:
        d["stated_count"] = max(1, min(5, int(re.search(r"\d+", a.get("count") or "1").group(0))))
    except Exception:
        d["stated_count"] = 1
    d["n"] = 1
    p = {}
    name = re.sub(r"\s+", " ", re.sub(r"[-'’]", " ", (a.get("full_name") or "").upper())).strip()
    if not re.fullmatch(r"[A-Z]+( [A-Z]+)+", name):
        errors.append("имя и фамилия должны быть латиницей, как в паспорте")
    p["full_name"] = name
    g = (a.get("gender") or "").lower()
    p["gender"] = "M" if g.startswith(("м", "m")) else "F" if g.startswith(("ж", "f", "w")) else None
    for k in ("birth", "passport_exp", "residence_exp"):
        v = parse_date(a.get(k) or "")
        if a.get(k) and not v:
            errors.append(f"«{TITLES[k]}»: не похоже на дату")
        p[k] = v.isoformat() if v else None
    country, _ = normalize_country(a.get("nationality") or "")
    if not country:
        errors.append("гражданство не распознано")
    p["nationality"] = country
    num = re.sub(r"[\s\-№#]", "", (a.get("passport") or "").upper())
    if not re.fullmatch(r"[A-Z0-9]{6,12}", num) or sum(ch.isdigit() for ch in num) < 5:
        errors.append("номер паспорта не похож на настоящий")
    p["passport"] = num
    if p.get("passport_exp") and date.fromisoformat(p["passport_exp"]) < date.today():
        errors.append("паспорт уже истёк")
    phone = re.sub(r"[^\d+]", "", a.get("phone") or "")
    d["phone"] = phone if phone.startswith("+") else ("+" + phone if phone else None)
    d["people"] = [p]
    d["form_extra"] = (a.get("others") or "").strip()[:1500] or None
    d["form_comment"] = (a.get("comment") or "").strip()[:500] or None
    return d, errors


async def handle_submission(bot: Bot, db, settings, payload: dict) -> dict:
    if not secrets.compare_digest(str(payload.get("secret", "")), os.getenv("FORM_SECRET", "")):
        return {"ok": False, "error": "bad secret"}
    a = _by_title(payload.get("answers") or {})
    tg = await db.kv_get(f"form:{(a.get('token') or '').strip()}")
    if not tg:
        log.warning("form submission without a known token: %s", a.get("token"))
        return {"ok": False, "error": "unknown token"}
    uid = int(tg)
    data, errors = validate(a)
    from . import orders, security
    from .checks import check_people, duplicate_passports
    hidden = {x.strip() for x in os.getenv("ORDER_EXCLUDE", "hun").split(",") if x.strip()}
    if "mission" in data and data["mission"] in hidden:
        errors.append("запись в эту страну временно недоступна")
    if not errors and "mission" in data:
        reason = await orders.order_block_reason(db, settings, uid, data["mission"])
        if reason:
            errors.append(reason)
    if errors:
        await bot.send_message(uid, "Получили твою форму, но в ней есть ошибки:\n" + "\n".join(f"• {html.escape(e)}" for e in errors)
                               + "\n\nИсправь и отправь форму ещё раз — или заполни заявку прямо в боте: /order")
        await db.log_event(uid, "form_error", "; ".join(errors)[:200])
        return {"ok": True, "errors": errors}
    applicant = {"people": data["people"], "phone": data["phone"], "tg_contact": None, "same_day": None,
                 "form_extra": data["form_extra"], "form_comment": data["form_comment"], "source": "google_form"}
    fee = await db.fee(data["mission"])  # one person
    oid = await db.create_order(uid, data["mission"], date.fromisoformat(data["date_from"]) if data["date_from"] else None,
                                date.fromisoformat(data["date_to"]) if data["date_to"] else None, security.encrypt(applicant),
                                destination=data["destination"], fee=fee, people=1)
    await db.log_event(uid, "order_new", f"#{oid} {data['destination']} form")
    await db.log_event(uid, "fstep", "submitted")
    warns = check_people(data["people"], date.fromisoformat(data["date_from"]) if data["date_from"] else None,
                         date.fromisoformat(data["date_to"]) if data["date_to"] else None)
    more = data.get("stated_count", 1) > 1
    text = (f"✅ Заявка из Google Формы получена: <b>#{oid}</b>, {html.escape(data['mission_name'])} (на 1 человека)."
            + ("\n\n👨‍👩‍👧 Ты указал, что едете несколько человек. На каждого нужна отдельная запись — "
               "оформи остальных через /order в боте (там можно сразу семьёй и со скидкой)." if more else "")
            + (f"\n\n⚠️ Обрати внимание:\n" + "\n".join(f"• {html.escape(w)}" for w in warns) if warns else "")
            + "\n\nСтатус — /myorders. Вопросы — /support")
    await bot.send_message(uid, text)
    from .requirements_by_country import requirements
    if requirements(data["mission"]).passport_scan:
        await bot.send_message(uid, "📷 Для этой страны визовый центр требует фото разворота паспорта — "
                                    "пришли его сюда в чат одним сообщением (фото или PDF).")
        await db.kv_set(f"await_scan:{uid}", str(oid))
    if fee > 0:
        await orders.issue_fee_invoice(bot, db, settings, oid)
    header = ("📝 Новая заявка из Google Формы (1 чел.)"
              + (f" · клиент указал {data['stated_count']} чел. — предложил оформить остальных в боте" if more else "")
              + (f" — ждём оплату сбора {fee:.2f} €" if fee else ""))
    dups = await duplicate_passports(db, data["people"], uid, exclude_order=oid)
    if dups:
        header += "\n⚠️ " + "; ".join(dups)
    if data["form_extra"]:
        header += f"\n👥 Остальные (как написал клиент): {html.escape(data['form_extra'][:500])}"
    if data["form_comment"]:
        header += f"\n💬 {html.escape(data['form_comment'])}"
    for a_id in settings.admin_targets:
        await orders.send_admin_card(bot, db, a_id, oid, settings, header=header)
    return {"ok": True, "order": oid}

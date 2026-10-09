"""Google Form as an alternative to the in-bot form.

The bot hands each client a personal prefilled link (hidden code -> Telegram user). A Google Apps Script trigger
posts every submission to /api/form; we validate it with the same parsers as the bot form and create the order.
The form has one section per applicant ("Заявитель N: <field>"), so families are collected in full.
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

log = logging.getLogger("gform")

# Fields that appear once per submission (section 0 + final section).
COMMON = {
    "country": "Страна поездки",
    "dates": "Даты, когда можешь подать документы",
    "phone": "Телефон (нужен визовому центру)",
    "comment": "Комментарий",
    "token": "Код заявки",
}
# Per-applicant fields. In the form each is titled "Заявитель N: <base>".
PERSON_FIELDS = {
    "full_name": "Имя и фамилия латиницей",
    "gender": "Пол",
    "birth": "Дата рождения",
    "nationality": "Гражданство",
    "passport": "Номер загранпаспорта",
    "passport_exp": "Паспорт действует до",
    "residence_exp": "ВНЖ Кипра действует до",
}
_APP_RE = re.compile(r"^\s*Заявител[ья]\s*(\d+)\s*[:：]\s*(.+)$", re.I)


def enabled() -> bool:
    return bool(os.getenv("FORM_PREFILL_URL") and os.getenv("FORM_SECRET"))


async def link_for(db, tg_id: int) -> str:
    token = secrets.token_urlsafe(9)
    await db.kv_set(f"form:{token}", str(tg_id))
    return os.environ["FORM_PREFILL_URL"].replace("TOKEN", token)


def _flat(v) -> str:
    return v if not isinstance(v, list) else ", ".join(map(str, v))


def _match_field(base: str, fields: dict) -> str | None:
    base = base.strip().lower()
    for field, title in fields.items():
        if base.startswith(title.lower()[:16]):
            return field
    return None


def _parse(answers: dict) -> tuple[dict, list[dict]]:
    """Split raw form answers into common fields + an ordered list of per-applicant raw dicts."""
    common = {}
    for key, title in COMMON.items():
        for k, v in answers.items():
            if k.strip().lower().startswith(title.lower()[:18]):
                common[key] = _flat(v)
                break
    people_raw: dict[int, dict] = {}
    for k, v in answers.items():
        m = _APP_RE.match(k.strip())
        if not m:
            continue
        idx = int(m.group(1))
        field = _match_field(m.group(2), PERSON_FIELDS)
        if field:
            people_raw.setdefault(idx, {})[field] = _flat(v)
    # Fallback for the old single-person form (no "Заявитель N:" prefix).
    if not people_raw:
        top = {}
        for k, v in answers.items():
            field = _match_field(k, PERSON_FIELDS)
            if field:
                top[field] = _flat(v)
        if top:
            people_raw[1] = top
    return common, [people_raw[i] for i in sorted(people_raw)]


def _validate_person(raw: dict, who: str) -> tuple[dict, list[str]]:
    from .parsing import normalize_country, parse_date
    errors, p = [], {}
    name = re.sub(r"\s+", " ", re.sub(r"[-'’]", " ", (raw.get("full_name") or "").upper())).strip()
    if not re.fullmatch(r"[A-Z]+( [A-Z]+)+", name):
        errors.append(f"{who}: имя и фамилия должны быть латиницей, как в паспорте")
    p["full_name"] = name
    g = (raw.get("gender") or "").lower()
    p["gender"] = "M" if g.startswith(("м", "m")) else "F" if g.startswith(("ж", "f", "w")) else None
    for k in ("birth", "passport_exp", "residence_exp"):
        v = parse_date(raw.get(k) or "")
        if raw.get(k) and not v:
            errors.append(f"{who}: «{PERSON_FIELDS[k]}» — не похоже на дату")
        p[k] = v.isoformat() if v else None
    country, _ = normalize_country(raw.get("nationality") or "")
    if not country:
        errors.append(f"{who}: гражданство не распознано")
    p["nationality"] = country
    num = re.sub(r"[\s\-№#]", "", (raw.get("passport") or "").upper())
    if not re.fullmatch(r"[A-Z0-9]{6,12}", num) or sum(ch.isdigit() for ch in num) < 5:
        errors.append(f"{who}: номер паспорта не похож на настоящий")
    p["passport"] = num
    if p.get("passport_exp") and date.fromisoformat(p["passport_exp"]) < date.today():
        errors.append(f"{who}: паспорт уже истёк")
    return p, errors


def validate(answers: dict) -> tuple[dict, list[str]]:
    """Normalize all answers into the bot's order data; return (data, errors)."""
    from .orders import DESTINATIONS
    from .parsing import parse_range
    common, people_raw = _parse(answers)
    errors, d = [], {}
    names = {v[0].lower(): k for k, v in DESTINATIONS.items() if not k.startswith("x_")}
    key = names.get((common.get("country") or "").strip().lower())
    if not key:
        errors.append("страна поездки не распознана")
    else:
        d["destination"], d["mission"] = key, DESTINATIONS[key][1]
        d["mission_name"], d["note"] = DESTINATIONS[key][0], DESTINATIONS[key][2]
    kind, d1, d2 = parse_range(common.get("dates") or "любые")
    if kind == "error":
        errors.append("даты не распознаны")
    d["date_from"], d["date_to"] = (d1.isoformat() if d1 else None), (d2.isoformat() if d2 else None)
    phone = re.sub(r"[^\d+]", "", common.get("phone") or "")
    d["phone"] = phone if phone.startswith("+") else ("+" + phone if phone else None)
    d["token"] = (common.get("token") or "").strip()
    d["form_comment"] = (common.get("comment") or "").strip()[:500] or None
    people = []
    if not people_raw:
        errors.append("нет данных ни на одного заявителя")
    for i, raw in enumerate(people_raw, 1):
        who = "Заявитель" if len(people_raw) == 1 else f"Заявитель {i}"
        p, errs = _validate_person(raw, who)
        errors.extend(errs)
        people.append(p)
    d["people"] = people
    return d, errors


async def handle_submission(bot: Bot, db, settings, payload: dict) -> dict:
    if not secrets.compare_digest(str(payload.get("secret", "")), os.getenv("FORM_SECRET", "")):
        return {"ok": False, "error": "bad secret"}
    answers = payload.get("answers") or {}
    data, errors = validate(answers)
    tg = await db.kv_get(f"form:{data.get('token', '')}")
    if not tg:
        log.warning("form submission without a known token: %s", data.get("token"))
        return {"ok": False, "error": "unknown token"}
    uid = int(tg)
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
        await bot.send_message(uid, "Получили твою форму, но в ней есть ошибки:\n"
                               + "\n".join(f"• {html.escape(e)}" for e in errors)
                               + "\n\nИсправь и отправь форму ещё раз — или заполни заявку прямо в боте: /order")
        await db.log_event(uid, "form_error", "; ".join(errors)[:200])
        return {"ok": True, "errors": errors}

    people = data["people"]
    n = max(1, len(people))
    applicant = {"people": people, "phone": data["phone"], "tg_contact": None,
                 "same_day": None, "form_comment": data["form_comment"], "source": "google_form"}
    d_from = date.fromisoformat(data["date_from"]) if data["date_from"] else None
    d_to = date.fromisoformat(data["date_to"]) if data["date_to"] else None
    fee = await db.fee(data["mission"]) * n
    oid = await db.create_order(uid, data["mission"], d_from, d_to, security.encrypt(applicant),
                                destination=data["destination"], fee=fee, people=n)
    disc = await db.best_discount(uid)
    if disc:
        await db.use_discount(disc["id"], oid)
        await db.update_order(oid, discount_id=disc["id"], discount_pct=disc["percent"])
    await db.log_event(uid, "order_new", f"#{oid} {data['destination']} form n={n}")
    await db.log_event(uid, "fstep", "submitted")
    warns = check_people(people, d_from, d_to)
    ppl = f" на {n} чел." if n > 1 else ""
    await bot.send_message(uid,
        f"✅ Заявка из формы принята: <b>#{oid}</b>, {html.escape(data['mission_name'])}{ppl}."
        + (f"\n\n⚠️ Обрати внимание:\n" + "\n".join(f"• {html.escape(w)}" for w in warns) if warns else "")
        + "\n\nСтатус — /myorders. Вопросы — /support"
        + (f"\n{settings.manager_contact}" if settings.manager_contact else ""))
    from .requirements_by_country import requirements
    if requirements(data["mission"]).passport_scan:
        await bot.send_message(uid, "📷 Для этой страны визовый центр требует фото разворота паспорта — "
                                    "пришли его сюда в чат одним сообщением (фото или PDF).")
        await db.kv_set(f"await_scan:{uid}", str(oid))
    if fee > 0:
        await orders.issue_fee_invoice(bot, db, settings, oid)
    header = (f"📝 Новая заявка из формы{ppl}" + (f" — ждём оплату сбора {fee:.2f} €" if fee else ""))
    dups = await duplicate_passports(db, people, uid, exclude_order=oid)
    if dups:
        header += "\n⚠️ " + "; ".join(dups)
    if data["form_comment"]:
        header += f"\n💬 {html.escape(data['form_comment'])}"
    for a_id in settings.admin_targets:
        await orders.send_admin_card(bot, db, a_id, oid, settings, header=header)
    return {"ok": True, "order": oid}

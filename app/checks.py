"""Automatic sanity checks of an order's data: shown to the client before sending and to the admin on the card."""
from __future__ import annotations

import re
from datetime import date, timedelta

FAKE_WORDS = {"test", "тест", "khui", "hui", "huy", "xuy", "pizda", "blya", "suka", "jopa", "zhopa", "asdf", "qwerty",
              "ivan ivanov", "petr petrov", "john doe", "sobach", "chlen", "fuck", "shit", "aaa", "bbb"}


def _age(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def check_people(people: list[dict], date_from: date | None, date_to: date | None) -> list[str]:
    """Human-readable warnings. Empty list = nothing suspicious."""
    warnings = []
    today = date.today()
    trip_end = date_to or date_from or today + timedelta(days=30)
    for p in people:
        who = p.get("full_name") or "?"
        if p.get("passport_exp"):
            exp = date.fromisoformat(p["passport_exp"])
            if exp < today + timedelta(days=180):
                warnings.append(f"{who}: паспорт истекает {exp:%d.%m.%Y} — визовый центр требует минимум 6 месяцев")
            elif exp < trip_end + timedelta(days=90):
                warnings.append(f"{who}: паспорт должен действовать 3 месяца после поездки (до {trip_end + timedelta(days=90):%d.%m.%Y})")
        if p.get("birth"):
            age = _age(date.fromisoformat(p["birth"]), today)
            if age < 18:
                warnings.append(f"{who}: несовершеннолетний ({age} лет) — нужны свидетельство о рождении и согласие родителей")
        name = (p.get("full_name") or "").lower()
        if any(w in name for w in FAKE_WORDS) or re.search(r"(.)\1{3,}", name):
            warnings.append(f"{who}: имя похоже на тестовое — проверь, что оно как в паспорте")
        if p.get("residence_receipt"):
            warnings.append(f"{who}: ВНЖ на продлении — не все визовые центры принимают ресипт")
    if date_from and (date_from - today).days < 3:
        warnings.append("Окно дат начинается меньше чем через 3 дня — шансов найти слот мало")
    return warnings


async def duplicate_passports(db, people: list[dict], tg_id: int, exclude_order: int | None = None) -> list[str]:
    """Passports that already appear in another client's open order."""
    from . import security
    rows = await db.pool.fetch(
        """SELECT id, tg_id, applicant FROM orders
           WHERE status IN ('awaiting_fee', 'new', 'in_progress', 'booked', 'paid') AND tg_id <> $1
             AND ($2::bigint IS NULL OR id <> $2) AND applicant <> ''""", tg_id, exclude_order)
    mine = {p.get("passport") for p in people if p.get("passport")}
    hits = []
    for r in rows:
        a = security.decrypt(r["applicant"])
        for p in a.get("people") or [a]:
            if p.get("passport") in mine:
                hits.append(f"паспорт {p['passport']} уже есть в заявке #{r['id']} другого клиента")
    return hits

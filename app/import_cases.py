"""Import the open case base of cases.visarea.travel (public endpoint /api/cases), shown with attribution."""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime

from curl_cffi.requests import AsyncSession

from .db import Database

log = logging.getLogger("import_cases")
SOURCE = "visarea"
URL = "https://cases.visarea.travel/api/cases"
COUNTRY = {
    "AT": "Австрия", "BE": "Бельгия", "BG": "Болгария", "CH": "Швейцария", "CZ": "Чехия", "DE": "Германия",
    "DK": "Дания", "EE": "Эстония", "ES": "Испания", "FI": "Финляндия", "FR": "Франция", "GR": "Греция",
    "HR": "Хорватия", "HU": "Венгрия", "IS": "Исландия", "IT": "Италия", "LI": "Лихтенштейн", "LT": "Литва",
    "LU": "Люксембург", "LV": "Латвия", "MT": "Мальта", "NL": "Нидерланды", "NO": "Норвегия", "PL": "Польша",
    "PT": "Португалия", "RO": "Румыния", "SE": "Швеция", "SI": "Словения", "SK": "Словакия",
}


def _date(v) -> date | None:
    if not v:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(v).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def _outcome(result: str | None) -> str:
    r = (result or "").strip().lower()
    if not r:
        return "pending"
    if r.startswith(("нет", "no", "отказ")) or "отказ" in r:
        return "refused"
    if any(w in r for w in ("жд", "рассм", "pending", "ещё не", "еще не")):
        return "pending"
    # "да, ...", "одобрили", and free-text answers that describe the visa granted ("дали на 3", "26 дней", "мульти")
    if r.startswith(("да", "yes")) or any(w in r for w in ("одобр", "дали", "выдал", "мульти", "мес", "дн", "недел", "год")):
        return "approved"
    return "pending"


def _validity(result: str | None) -> str | None:
    """What the visa was granted for: 'под даты поездки (+- 5 дней)', 'от 1 до 3 месяцев', free text."""
    r = (result or "").strip()
    r = re.sub(r"^(да|yes)(?![а-яёa-z])[,.:\s]*", "", r, flags=re.I).strip()
    return (r[:1].upper() + r[1:])[:80] if r else None


def _kind(v: str | None) -> str | None:
    t = (v or "").lower()
    if "мульти" in t or "мног" in t or "multi" in t:
        return "multi"
    if "одно" in t or "single" in t or "разов" in t:
        return "single"
    return None


async def import_visarea(db: Database) -> str:
    async with AsyncSession(timeout=40, impersonate="chrome") as s:
        r = await s.get(URL, headers={"accept": "application/json"})
    data = r.json()
    items = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"unexpected payload: {str(data)[:200]}")
    log.info("visarea keys: %s", sorted(items[0].keys()) if items else [])
    seen = Counter(_outcome(x.get("result")) for x in items)
    log.info("visarea results raw: %s", Counter((x.get("result") or "")[:40] for x in items).most_common(15))
    new = 0
    for x in items:
        ext = str(x.get("id"))
        country = COUNTRY.get((x.get("country_code") or "").upper(), x.get("country_code") or "?")
        result = x.get("result")
        validity = _validity(result) if _outcome(result) == "approved" else None
        extra = {k: x.get(k) for k in ("citizenship", "docs", "tenure", "requested_term", "group_size", "tickets",
                                       "hotels", "extra_docs", "violations", "sch_same", "sch_other") if x.get(k)}
        is_new = await db.upsert_external_case(
            SOURCE, ext, country=country, submitted_on=_date(x.get("submitted_at")), decided_on=_date(x.get("received_at")),
            outcome=_outcome(result), visa_kind=_kind(x.get("visa_type")), validity=validity,
            comment=(x.get("comment") or x.get("comments") or None), publish=True, status="published",
            extra=json.dumps(extra, ensure_ascii=False))
        new += is_new
    summary = f"Импорт cases.visarea.travel: {len(items)} кейсов, новых {new}. Итоги: {dict(seen)}"
    log.info(summary)
    return summary

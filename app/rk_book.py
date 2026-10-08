"""Auto-book a German embassy (RK-Termin) appointment. No login, only a captcha we already solve.

Flow: solve captcha -> month -> pick a free day in the client's window -> open the time slot -> fill the
applicant form -> submit -> the embassy emails a confirmation link which must be clicked to finalize.
We read that email from the service mailbox and open the link.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import logging
import random
import re
from datetime import date, datetime, timezone
from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession

from .rktermin import (ATTR_RE, BASE, CAPTCHA_RE, DAY_HREF_RE, INPUT_RE, LOCATION, REALM, RkTerminClient,
                       parse_days, solve_captcha)

log = logging.getLogger("rk_book")
RKROOT = "https://service2.diplo.de/rktermin/"
TIME_LINK_RE = re.compile(r'href="([^"]*appointment_showForm\.do[^"]*)"[^>]*>\s*(\d{1,2}:\d{2})', re.I | re.S)
FORM_RE = re.compile(r'<form[^>]*name="appointment_newAppointmentForm"[^>]*action="([^"]+)"(.*?)</form>', re.I | re.S)
SUCCESS_RE = re.compile(r"(erfolgreich|successfully|confirmation|bestätigung|e-?mail)", re.I)


class BookingError(Exception):
    pass


async def inspect_form(rk, date_from=None, date_to=None) -> str:
    """Dry run: go to the booking form for the next free day and return its fields, WITHOUT submitting."""
    async with AsyncSession(proxies=rk.proxies, timeout=40, impersonate="chrome124") as s:
        cat = await rk._category(s)
        month = ""
        for _ in range(6):
            month = await rk._pass_captcha(s, cat)
            if month:
                break
            await asyncio.sleep(random.uniform(2, 4))
        if not month:
            return "капча не пройдена"
        days = parse_days(month)
        day = _pick(days, date_from, date_to) or (sorted(days)[0] if days else None)
        if not day:
            return "нет свободных дней сейчас — форму покажу, когда появится слот"
        href = _day_href(month, day)
        day_page = await rk._get(s, urljoin(RKROOT, href))
        times = TIME_LINK_RE.findall(day_page)
        if not times:
            return f"день {day} без свободного времени"
        form_page = await rk._get(s, urljoin(RKROOT, htmllib.unescape(times[0][0])))
        action, fields = _parse_form(form_page)
        if not action:
            return "форма не распознана (см. логи)"
        log.info("RKFORM action=%s fields=%s", action, list(fields))
        return (f"🔬 Форма записи Германии ({day:%d.%m} {times[0][1]}):\n"
                + "\n".join(f"• {k}" for k in fields) + f"\n\nПолей: {len(fields)}")


async def book(rk: RkTerminClient, applicant: dict, date_from: date | None, date_to: date | None,
               mailbox_wait=None) -> dict:
    """Try to book one slot for `applicant` within the window. Returns {ok, when, confirmed, detail}."""
    proxies = rk.proxies
    async with AsyncSession(proxies=proxies, timeout=40, impersonate="chrome124") as s:
        cat = await rk._category(s)
        month = ""
        for attempt in range(6):
            month = await rk._pass_captcha(s, cat)
            if month:
                break
            await asyncio.sleep(random.uniform(2, 4))
        if not month:
            raise BookingError("капча не пройдена")
        days = parse_days(month)
        # walk forward a couple of months if nothing in this one
        hop = 0
        while not _pick(days, date_from, date_to) and hop < 3:
            nxt = re.findall(r'href="([^"]*appointment_showMonth\.do[^"]*)"', month)
            if not nxt:
                break
            month = await rk._get(s, urljoin(RKROOT, htmllib.unescape(nxt[-1])))
            if CAPTCHA_RE.search(month):
                month = await rk._pass_captcha(s, cat) or month
            days = parse_days(month)
            hop += 1
        day = _pick(days, date_from, date_to)
        if not day:
            return {"ok": False, "detail": "нет свободных дней в окне"}

        day_href = _day_href(month, day)
        if not day_href:
            return {"ok": False, "detail": f"нет ссылки на день {day}"}
        day_page = await rk._get(s, urljoin(RKROOT, day_href))
        times = TIME_LINK_RE.findall(day_page)
        if not times:
            return {"ok": False, "detail": f"нет свободного времени на {day}"}
        form_href, when = times[0]
        form_page = await rk._get(s, urljoin(RKROOT, htmllib.unescape(form_href)))

        action, fields = _parse_form(form_page)
        if not action:
            log.warning("form not parsed: %s", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", form_page))[:800])
            return {"ok": False, "detail": "форма записи не распознана"}
        _fill(fields, applicant)
        # solve the form captcha if present
        img = CAPTCHA_RE.search(form_page)
        if img:
            import base64
            code = solve_captcha(base64.b64decode(re.sub(r"\s+", "", img.group(1))))
            for k in list(fields):
                if "captcha" in k.lower() and "text" in k.lower():
                    fields[k] = code or ""
        fields["action:appointment_addAppointment"] = "Book this appointment"
        await asyncio.sleep(random.uniform(1.5, 3))
        r = await s.post(urljoin(RKROOT, htmllib.unescape(action)), data=fields,
                         headers={"referer": urljoin(RKROOT, htmllib.unescape(form_href))}, impersonate="chrome124")
        body = r.text or ""
        if "global-error" in body or CAPTCHA_RE.search(body):
            err = re.search(r"global-error[^>]*>(.*?)</", body, re.S)
            return {"ok": False, "detail": "отклонено: " + (re.sub(r"<[^>]+>", " ", err.group(1)).strip()[:150] if err else "см. логи")}

        when_str = f"{day:%d.%m.%Y} {when}"
        result = {"ok": True, "when": when_str, "confirmed": False,
                  "detail": "форма отправлена; посольство прислало письмо для подтверждения"}
        # RK-Termin finalizes only after the email link is clicked
        if mailbox_wait:
            link = await mailbox_wait(sender_like="diplo", pattern=r"(https://service2\.diplo\.de/rktermin[^\s\"'<>]+)",
                                      since=datetime.now(timezone.utc), timeout=240)
            if link:
                conf = await s.get(link, impersonate="chrome124")
                result["confirmed"] = bool(SUCCESS_RE.search(conf.text or ""))
                result["detail"] = "запись подтверждена по ссылке из письма" if result["confirmed"] else \
                    "ссылка из письма открыта, подтверждение не распознано — проверь почту"
        return result


def _pick(days, d_from, d_to):
    today = date.today()
    for d in sorted(days):
        if d < today:
            continue
        if (d_from is None or d >= d_from) and (d_to is None or d <= d_to):
            return d
    return None


def _day_href(month_page: str, day: date) -> str | None:
    for m in DAY_HREF_RE.finditer(month_page):
        if datetime.strptime(m.group(2), "%d.%m.%Y").date() == day:
            return htmllib.unescape(m.group(1))
    return None


def _parse_form(page: str):
    m = FORM_RE.search(page)
    if not m:
        return None, {}
    action, inner = m.group(1), m.group(2)
    fields = {}
    for raw in INPUT_RE.findall(inner):
        a = {k.lower(): htmllib.unescape(v) for k, v in ATTR_RE.findall(raw)}
        name, typ = a.get("name"), a.get("type", "text").lower()
        if name and typ in ("hidden", "text", "email", "tel", "radio", "checkbox"):
            if typ in ("radio", "checkbox") and "checked" not in raw.lower():
                continue
            fields[name] = a.get("value", "")
    for sel in re.findall(r"<select[^>]*name=\"([^\"]+)\"", inner):
        fields.setdefault(sel, "")
    return action, fields


def _fill(fields: dict, a: dict):
    """Map applicant data onto RK-Termin field names (they vary; match by keyword)."""
    name = a.get("full_name") or ""
    first, _, last = name.partition(" ")
    mapping = {
        ("lastname", "surname", "nachname", "familyname"): last or name,
        ("firstname", "vorname", "givenname"): first,
        ("email", "mail"): a.get("email") or "",
        ("phone", "telefon", "mobile"): a.get("phone") or "",
        ("passport", "pass", "reisepass", "documentnumber"): a.get("passport") or "",
        ("nationality", "staatsangeh", "citizenship"): a.get("nationality") or "",
        ("birth", "geburt", "dateofbirth"): a.get("birth") or "",
    }
    for k in fields:
        kl = k.lower()
        for keys, val in mapping.items():
            if any(key in kl for key in keys) and val:
                fields[k] = val
                break

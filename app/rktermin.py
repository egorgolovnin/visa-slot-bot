"""German Foreign Office appointment system (RK-Termin), Nicosia embassy.

The German embassy in Nicosia handles Schengen visas for Germany, the Netherlands, Belgium,
Denmark, Norway, Iceland and Luxembourg. The calendar is public: no login, but each visit shows
a 6-character image captcha, solved locally with ddddocr (a wrong guess just means a new try).
"""
from __future__ import annotations

import asyncio
import base64
import html as htmllib
import logging
import os
import random
import re
from collections import Counter
from datetime import date, datetime

from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession

from .vfs import Blocked, Slot

log = logging.getLogger("rktermin")

BASE = "https://service2.diplo.de/rktermin/extern"
LOCATION, REALM = "niko", "926"
CENTRE = "Посольство Германии, Никосия"
CATEGORY = "Шенген до 90 дней (DE, NL, BE, DK, NO, IS, LU)"
NO_SLOTS = "no appointments available"
MAX_CAPTCHA_TRIES = 6
MONTHS_AHEAD = 3

CAPTCHA_RE = re.compile(r"url\(['\"]?data:image/[a-z]+;base64,([A-Za-z0-9+/=\s]+)", re.I)
FORM_RE = re.compile(r"<form[^>]*id=\"appointment_captcha_month\"[^>]*>(.*?)</form>", re.I | re.S)
ACTION_RE = re.compile(r"<form[^>]*id=\"appointment_captcha_month\"[^>]*action=\"([^\"]+)\"", re.I)
INPUT_RE = re.compile(r"<input([^>]*)>", re.I)
ATTR_RE = re.compile(r"(\w+)=\"([^\"]*)\"")
DAY_RE = re.compile(r"appointment_showDay\.do[^\"']*?dateStr=(\d{2}\.\d{2}\.\d{4})")
NEXT_MONTH_RE = re.compile(r"href=\"([^\"]*appointment_showMonth\.do[^\"]*dateStr=(\d{2}\.\d{2}\.\d{4})[^\"]*)\"")
CATEGORY_RE = re.compile(r"categoryId=(\d+)")

_models: tuple | None = None


def _solvers() -> tuple:
    global _models
    if _models is None:
        import ddddocr  # heavy import, load once
        _models = (ddddocr.DdddOcr(show_ad=False), ddddocr.DdddOcr(show_ad=False, beta=True))
    return _models


def _solver():
    return _solvers()[0]


def _variants(image: bytes) -> list[bytes]:
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return [image]
    _, otsu = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    median = cv2.medianBlur(img, 3)
    big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return [image] + [cv2.imencode(".png", v)[1].tobytes() for v in (otsu, median, big)]


def solve_captcha(image: bytes) -> str | None:
    """Vote across 2 OCR models x 4 preprocessings. The captcha is always 6 chars [a-z0-9];
    a hook on the right edge often adds a 7th char, so 7-char reads also vote with their 6-char cuts."""
    votes: Counter[str] = Counter()
    for model in _solvers():
        for variant in _variants(image):
            text = re.sub(r"[^a-z0-9]", "", model.classification(variant).lower())
            # every 6-char window of a read votes once: noise is usually an extra char at an edge
            for i in range(0, max(0, len(text) - 6) + 1):
                if len(text) - 6 <= 2:
                    votes[text[i:i + 6]] += 1
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def find_schengen_category(page: str) -> str | None:
    """The category whose surrounding text mentions Schengen (not the north-Nicosia office)."""
    for m in CATEGORY_RE.finditer(page):
        window = re.sub(r"<[^>]+>", " ", page[max(0, m.start() - 600): m.end() + 600])
        if "Schengen" in window and "Ayii Omologites" in window:
            return m.group(1)
    for m in CATEGORY_RE.finditer(page):
        window = re.sub(r"<[^>]+>", " ", page[m.start(): m.end() + 600])
        if "Schengen" in window:
            return m.group(1)
    return None


def parse_form(page: str) -> tuple[str, dict[str, str]] | None:
    form = FORM_RE.search(page)
    action = ACTION_RE.search(page)
    if not form or not action:
        return None
    fields: dict[str, str] = {}
    for raw in INPUT_RE.findall(form.group(1)):
        attrs = {k.lower(): htmllib.unescape(v) for k, v in ATTR_RE.findall(raw)}
        name = attrs.get("name")
        if name and attrs.get("type", "text").lower() in ("hidden", "submit", "text"):
            fields[name] = attrs.get("value", "")
    return htmllib.unescape(action.group(1)), fields


def parse_days(page: str) -> list[date]:
    days = {datetime.strptime(d, "%d.%m.%Y").date() for d in DAY_RE.findall(page)}
    return sorted(days)


class RkTerminClient:
    def __init__(self, proxy_url: str | None):
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.category_id: str | None = os.getenv("RK_CATEGORY_ID") or "1955"

    def page_url(self) -> str:
        cat = f"&categoryId={self.category_id}" if self.category_id else ""
        return (f"{BASE}/{'appointment_showMonth' if cat else 'choose_categoryList'}.do"
                f"?locationCode={LOCATION}&realmId={REALM}{cat}&request_locale=en")

    async def _get(self, s: AsyncSession, url: str) -> str:
        r = await s.get(url, impersonate="chrome")
        if r.status_code != 200:
            raise Blocked(f"rktermin {r.status_code} on {url[:80]}")
        return r.text

    async def _category(self, s: AsyncSession) -> str:
        if self.category_id:
            return self.category_id
        page = await self._get(s, f"{BASE}/choose_categoryList.do?locationCode={LOCATION}&realmId={REALM}&request_locale=en")
        cat = find_schengen_category(page)
        if not cat:
            log.warning("category not found, page: %s", re.sub(r"\s+", " ", page)[:1500])
            raise Blocked("rktermin: Schengen category not found")
        self.category_id = cat
        log.info("rktermin category id = %s", cat)
        return cat

    async def _pass_captcha(self, s: AsyncSession, cat: str) -> str:
        url = f"{BASE}/appointment_showMonth.do?locationCode={LOCATION}&realmId={REALM}&categoryId={cat}&request_locale=en"
        page = await self._get(s, url)
        img = CAPTCHA_RE.search(page)
        form = parse_form(page)
        if not img or not form:
            if NO_SLOTS in page or DAY_RE.search(page):
                return page  # no captcha shown this time
            log.warning("captcha page not understood: %s", re.sub(r"\s+", " ", page)[:1500])
            raise Blocked("rktermin: captcha page not understood")
        raw_img = base64.b64decode(re.sub(r"\s+", "", img.group(1)))
        answer = solve_captcha(raw_img)
        if not answer:
            log.info("captcha OCR unusable: %r", _solver().classification(raw_img))
            return ""
        action, fields = form
        if not getattr(self, "_form_logged", False):
            self._form_logged = True
            log.info("captcha form action=%r fields=%r", action, {k: v[:40] for k, v in fields.items()})
        for name in list(fields):
            if "captcha" in name.lower() and "text" in name.lower():
                fields[name] = answer
        if not any("captcha" in n.lower() for n in fields):
            fields["captchaText"] = answer
        # the form has three submit buttons (new picture / continue / cancel): send only "continue",
        # otherwise the server runs "load another picture" and every answer looks wrong
        fields = {k: v for k, v in fields.items() if not k.startswith("action:")}
        fields["action:appointment_showMonth"] = "Continue"
        target = f"{BASE}/appointment_showMonth.do"
        await asyncio.sleep(random.uniform(1.5, 3))
        r = await s.post(target, data=fields, impersonate="chrome", headers={"referer": url})
        if r.status_code != 200:
            raise Blocked(f"rktermin POST {r.status_code}")
        text = r.text
        has_result = NO_SLOTS in text or DAY_RE.search(text) or NEXT_MONTH_RE.search(text)
        if "global-error" in text or not has_result:
            err = re.search(r"global-error[^>]*>(.*?)</", text, re.S)
            log.info("captcha %r rejected: %s", answer,
                     re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", err.group(1)))[:200] if err else
                     re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:600])
            return ""
        return text

    async def fetch(self) -> tuple[list[Slot], str]:
        try:
            return await self._fetch()
        except Blocked:
            raise
        except Exception as e:  # network errors, timeouts
            raise Blocked(f"rktermin network: {type(e).__name__}: {str(e)[:120]}") from e

    async def _fetch(self) -> tuple[list[Slot], str]:
        async with AsyncSession(proxies=self.proxies, timeout=30) as s:
            cat = await self._category(s)
            page = ""
            for attempt in range(1, MAX_CAPTCHA_TRIES + 1):
                page = await self._pass_captcha(s, cat)
                if page:
                    log.info("rktermin captcha passed on try %d", attempt)
                    break
                await asyncio.sleep(random.uniform(2, 4))
            if not page:
                raise Blocked(f"rktermin: captcha not solved in {MAX_CAPTCHA_TRIES} tries")

            days = parse_days(page)
            seen_months = 1
            while not days and seen_months < MONTHS_AHEAD:
                nxt = [m for m in NEXT_MONTH_RE.finditer(page)]
                if not nxt:
                    break
                href = htmllib.unescape(nxt[-1].group(1))
                url = urljoin("https://service2.diplo.de/rktermin/", href)
                await asyncio.sleep(random.uniform(1.5, 3))
                page = await self._get(s, url)
                if CAPTCHA_RE.search(page):
                    break  # session asks for a new captcha; stop here
                days = parse_days(page)
                seen_months += 1

            if not days and NO_SLOTS not in page:
                log.info("rktermin page without days: %s", re.sub(r"\s+", " ", page)[:1200])
            earliest = days[0] if days else None
            slot = Slot("deu", CENTRE, CATEGORY, earliest, earliest.isoformat() if earliest else "")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            log.info("rktermin: %d free days, earliest=%s, months checked=%d", len(days), earliest, seen_months)
            return [slot], stamp

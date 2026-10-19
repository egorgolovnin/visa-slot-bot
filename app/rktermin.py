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
MAX_CAPTCHA_TRIES = 8
MONTHS_AHEAD = 3

CAPTCHA_RE = re.compile(r"url\(['\"]?data:image/[a-z]+;base64,([A-Za-z0-9+/=\s]+)", re.I)
FORM_RE = re.compile(r"<form[^>]*id=\"appointment_captcha_month\"[^>]*>(.*?)</form>", re.I | re.S)
ACTION_RE = re.compile(r"<form[^>]*id=\"appointment_captcha_month\"[^>]*action=\"([^\"]+)\"", re.I)
INPUT_RE = re.compile(r"<input([^>]*)>", re.I)
ATTR_RE = re.compile(r"(\w+)=\"([^\"]*)\"")
DAY_RE = re.compile(r"appointment_showDay\.do[^\"']*?dateStr=(\d{2}\.\d{2}\.\d{4})")
DAY_HREF_RE = re.compile(r"href=\"([^\"]*appointment_showDay\.do[^\"]*dateStr=(\d{2}\.\d{2}\.\d{4})[^\"]*)\"")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
RK_TIME_BUDGET = int(os.getenv("RK_TIME_BUDGET", "12"))  # max NEW day-pages to open per cycle for times (rest cached)
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
    _, big_otsu = cv2.threshold(cv2.medianBlur(big, 3), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 4)
    denoised = cv2.morphologyEx(otsu, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))  # drop speckle noise
    outs = (otsu, median, big, big_otsu, adaptive, denoised)
    return [image] + [cv2.imencode(".png", v)[1].tobytes() for v in outs]


def solve_captcha(image: bytes) -> str | None:
    """Vote across 2 OCR models x 4 preprocessings. The captcha is always 6 chars [a-z0-9];
    a hook on the right edge often adds a 7th char, so 7-char reads also vote with their 6-char cuts."""
    votes: Counter[str] = Counter()
    for model in _solvers():
        for variant in _variants(image):
            text = re.sub(r"[^a-z0-9]", "", model.classification(variant).lower())
            # the captcha is exactly 6 chars: a read of 6-8 chars votes with each 6-char window
            # (noise is usually an extra char at an edge); shorter reads are useless and never vote
            if 6 <= len(text) <= 8:
                for i in range(len(text) - 5):
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
        self.last_free_days = 0
        self.last_times: dict[date, list[str]] = {}
        self.new_times: dict[date, list[str]] = {}   # times that appeared on an ALREADY-open day this cycle
        self._times_at: dict[date, float] = {}       # when each day's times were last read (for round-robin refresh)
        self._day_logged = False
        self.form_report: str | None = None    # filled once, when the booking form is first seen
        self._form_probed = False
        self.session: AsyncSession | None = None  # reused across fetches so the captcha-passed cookie sticks
        self._force_direct = False  # diplo isn't Cloudflare-IP-gated; if the proxy flakes, go direct

    def _session(self) -> AsyncSession:
        if self.session is None:
            proxies = None if self._force_direct else self.proxies
            self.session = AsyncSession(proxies=proxies, timeout=30)
        return self.session

    async def close(self) -> None:
        if self.session is not None:
            try:
                await self.session.close()
            finally:
                self.session = None

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

    async def _read_times(self, s: AsyncSession, hrefs: dict[date, str], days: list[date]) -> dict[date, list[str]]:
        """Read appointment times (e.g. 09:00, 09:30) per free day and REFRESH them across cycles so a
        new time appearing on an already-open day is caught. We don't re-read every day every cycle
        (that would hammer diplo) — each cycle we (re)read at most RK_TIME_BUDGET day pages, picking
        never-read days first, then the ones read longest ago (round-robin). Times added on a day we
        already knew are recorded in self.new_times for the poller to alert on."""
        import time as _time
        days_set = set(days)
        prev = {d: t for d, t in self.last_times.items() if d in days_set}   # carry over still-free days
        self._times_at = {d: ts for d, ts in self._times_at.items() if d in days_set}
        now = _time.time()
        # priority: never-read days (ts 0) first, then stale ones (oldest read first)
        candidates = sorted((d for d in days if d in hrefs), key=lambda d: self._times_at.get(d, 0.0))
        cache = dict(prev)
        new_times: dict[date, list[str]] = {}
        for d in candidates[:RK_TIME_BUDGET]:
            await asyncio.sleep(random.uniform(1.5, 3))
            try:
                page = await self._get(s, urljoin("https://service2.diplo.de/rktermin/", hrefs[d]))
            except Exception as e:
                log.info("day page %s failed: %s", d, e)
                break
            if CAPTCHA_RE.search(page):
                break
            # times live in links to the booking form; fall back to any HH:MM in the page body
            links = re.findall(r"<a[^>]*appointment_showForm[^>]*>(.*?)</a>", page, re.S | re.I)
            source = " ".join(links) if links else re.sub(r"<[^>]+>", " ", page)
            times = sorted({f"{int(h):02d}:{mi}" for h, mi in TIME_RE.findall(source) if 6 <= int(h) <= 19})
            added = sorted(set(times) - set(prev.get(d, [])))
            if d in prev and added:   # a brand-new DAY is handled by the date-open alert, not here
                new_times[d] = added
            if not self._form_probed:
                await self._probe_form(s, page)
            if not self._day_logged:
                self._day_logged = True
                log.info("day page sample %s: links=%d times=%s text=%s", d, len(links), times,
                         re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))[:800])
            cache[d] = times
            self._times_at[d] = now
        self.new_times = new_times
        return cache

    async def _probe_form(self, s: AsyncSession, day_page: str) -> None:
        """Open (never submit) the booking form behind the first free time and record its fields."""
        m = re.search(r'href="([^"]*appointment_showForm\.do[^"]*)"', day_page)
        if not m:
            return
        self._form_probed = True
        try:
            await asyncio.sleep(random.uniform(1.5, 3))
            form = await self._get(s, urljoin("https://service2.diplo.de/rktermin/", htmllib.unescape(m.group(1))))
        except Exception as e:
            log.warning("form probe failed: %s", e)
            return
        fields = []
        for raw in INPUT_RE.findall(form) + re.findall(r"<(?:select|textarea)([^>]*)>", form, re.I):
            a = {k.lower(): htmllib.unescape(v) for k, v in ATTR_RE.findall(raw)}
            if a.get("name") and a.get("type", "text").lower() not in ("hidden", "submit"):
                fid = a.get("id", "")
                label = re.search(rf'<label[^>]*for="{re.escape(fid)}"[^>]*>(.*?)</label>', form, re.S) if fid else None
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label.group(1))).strip() if label else ""
                fields.append(f"{a['name']} ({a.get('type', 'select/text')}){': ' + text if text else ''}")
        captcha = "капча есть" if CAPTCHA_RE.search(form) else "капчи нет"
        self.form_report = (f"Поля формы записи ({captcha}):\n" + "\n".join(f"• {f}" for f in fields)) if fields else None
        log.info("FORM PROBE: %s | text: %s", fields, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", form))[:1500])

    _PROXY_ERR = ("proxy", "connect", "tunnel", "failed to perform", "timed out", "timeout")

    async def fetch(self) -> tuple[list[Slot], str]:
        try:
            return await self._fetch()
        except Blocked as e:
            await self.close()  # drop the session so a block/captcha starts fresh next time
            # diplo doesn't need the residential proxy; if the proxy flaked, retry once DIRECT so
            # Germany doesn't go blind during a proxy outage. Stay direct while it keeps working.
            if self.proxies and not self._force_direct and any(s in str(e).lower() for s in self._PROXY_ERR):
                self._force_direct = True
                try:
                    return await self._fetch()
                except Blocked:
                    await self.close()
                    self._force_direct = False  # direct didn't help either — proxy again next time
                    raise
            if self._force_direct:
                self._force_direct = False  # direct mode started failing — swing back to the proxy next cycle
            raise
        except Exception as e:  # network errors, timeouts
            await self.close()
            raise Blocked(f"rktermin network: {type(e).__name__}: {str(e)[:120]}") from e

    async def _fetch(self) -> tuple[list[Slot], str]:
        if True:
            s = self._session()  # persistent: keeps the captcha-passed cookie between cycles
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

            # Walk up to MONTHS_AHEAD months and COLLECT every free day across the whole window
            # (plus each day's link, to open it for the times) — don't stop at the first non-empty month.
            days: set[date] = set()
            hrefs: dict[date, str] = {}
            months_seen = 0
            while months_seen < MONTHS_AHEAD:
                months_seen += 1
                days.update(parse_days(page))
                for m in DAY_HREF_RE.finditer(page):
                    dd = datetime.strptime(m.group(2), "%d.%m.%Y").date()
                    hrefs.setdefault(dd, htmllib.unescape(m.group(1)))
                nxt = [m for m in NEXT_MONTH_RE.finditer(page)]
                if not nxt or months_seen >= MONTHS_AHEAD:
                    break
                url = urljoin("https://service2.diplo.de/rktermin/", htmllib.unescape(nxt[-1].group(1)))
                await asyncio.sleep(random.uniform(1.5, 3))
                page = await self._get(s, url)
                if CAPTCHA_RE.search(page):
                    break  # session wants a fresh captcha; stop walking, keep what we already collected

            day_list = sorted(days)
            if day_list:
                self.last_times = await self._read_times(s, hrefs, day_list)
            else:
                # reset both, or a stale new_times from a prior cycle would re-alert on an empty one
                self.last_times, self.new_times = {}, {}
                log.info("rktermin: no free days across %d month(s)", months_seen)
            self.last_free_days = len(day_list)
            # One Slot PER free day so the pipeline treats every date as its own slot: a newly-appeared
            # date (even one LATER than the current earliest) fires OPENED, a consumed date fires CLOSED.
            # The date is encoded into `category` because slots are keyed on (centre, category).
            slots = [Slot("deu", CENTRE, f"{CATEGORY} · {d.strftime('%d.%m.%Y')}", d, d.isoformat()) for d in day_list]
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            log.info("rktermin: %d free days across %d month(s), earliest=%s, times=%s",
                     len(day_list), months_seen, day_list[0] if day_list else None,
                     {d.isoformat(): t for d, t in self.last_times.items()})
            return slots, stamp

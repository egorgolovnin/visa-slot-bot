"""Greek embassy in Nicosia (also handles Malta) books Schengen appointments on SuperSaaS.

The calendar needs a logged-in account (free signup). Access is gated by a simple arithmetic
anti-bot question ("3 + 1 = ") which we solve automatically. The bot only looks at the 'free
slots' view; it never books. Enabled when SUPERSAAS_EMAIL and SUPERSAAS_PASSWORD are set.
"""
from __future__ import annotations

import html as htmllib
import logging
import os
import re
from datetime import date, datetime

from curl_cffi.requests import AsyncSession

from .vfs import Blocked, Slot

log = logging.getLogger("supersaas")

ACCOUNT, SCHEDULE = "EmbassyofGreeceinCyprus", "VISA"
BASE = "https://www.supersaas.com"
CENTRE = "Посольство Греции, Никосия"
CATEGORY = "Шенген (Греция, Мальта)"

FORM_RE = re.compile(r"<form[^>]*action=\"([^\"]*login[^\"]*)\"[^>]*>(.*?)</form>", re.I | re.S)
INPUT_RE = re.compile(r"<input([^>]*)>", re.I)
ATTR_RE = re.compile(r"([\w\-\[\]]+)=\"([^\"]*)\"")
ANY_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.I | re.S)
# anti-bot gate, e.g. "Please answer this question: 3 + 1 ="
QUESTION_RE = re.compile(r"answer this question\D{0,40}?(\d+)\s*([+\-x*×])\s*(\d+)\s*=", re.I | re.S)


def _arith(a: str, op: str, b: str) -> int | None:
    x, y = int(a), int(b)
    return {"+": x + y, "-": x - y, "*": x * y, "x": x * y, "×": x * y}.get(op)
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# "Mon 12 Oct 2026 09:00", "12 Oct 2026", "Oct 12, 2026", "12/10/2026"
DATE_PATTERNS = (
    (re.compile(rf"\b(\d{{1,2}}) ({MONTHS})[a-z]* (\d{{4}})\b"), lambda m: f"{m[1]} {m[2]} {m[3]}", "%d %b %Y"),
    (re.compile(rf"\b({MONTHS})[a-z]* (\d{{1,2}}),? (\d{{4}})\b"), lambda m: f"{m[2]} {m[1]} {m[3]}", "%d %b %Y"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: f"{m[1]}/{m[2]}/{m[3]}", "%d/%m/%Y"),
)


def enabled() -> bool:
    return bool(os.getenv("SUPERSAAS_EMAIL") and os.getenv("SUPERSAAS_PASSWORD"))


def text_of(page: str) -> str:
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", page)))


def parse_free_dates(page: str) -> list[date]:
    """Dates mentioned in the free-slots list (the view lists only bookable slots)."""
    text = text_of(page)
    found: set[date] = set()
    today = date.today()
    for rx, fmt_input, fmt in DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                d = datetime.strptime(fmt_input(m), fmt).date()
            except ValueError:
                continue
            if d >= today:
                found.add(d)
    return sorted(found)


def no_free_slots(page: str) -> bool:
    t = text_of(page).lower()
    return any(p in t for p in ("no available", "no free", "nothing available", "fully booked", "no space"))


class SuperSaasClient:
    def __init__(self, proxy_url: str | None):
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.email = os.getenv("SUPERSAAS_EMAIL", "")
        self.password = os.getenv("SUPERSAAS_PASSWORD", "")
        self.session: AsyncSession | None = None
        self._raw_logged = False

    def page_url(self) -> str:
        return f"{BASE}/schedule/{ACCOUNT}/{SCHEDULE}"

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _answer_challenge(self, r):
        """If the page is the arithmetic anti-bot gate, solve it and submit; return the new response.
        Loops a few times in case a fresh question is shown after a wrong/again prompt."""
        for _ in range(3):
            m = QUESTION_RE.search(r.text)
            if not m:
                if "this question" in r.text.lower():
                    i = r.text.lower().find("this question")
                    log.warning("supersaas question RAW html: %s", r.text[max(0, i - 80):i + 500])
                return r
            answer = _arith(*m.groups())
            if answer is None:
                return r
            action, fields, answer_field = None, {}, None
            for fm in ANY_FORM_RE.finditer(r.text):
                block = fm.group(1)
                inputs, afield = {}, None
                for raw in INPUT_RE.findall(block):
                    a = {k.lower(): htmllib.unescape(v) for k, v in ATTR_RE.findall(raw)}
                    nm, typ = a.get("name"), a.get("type", "text").lower()
                    if not nm or typ in ("submit", "button", "image"):
                        continue
                    if typ in ("text", "number", "tel") and afield is None:
                        afield, inputs[nm] = nm, str(answer)
                    else:
                        inputs[nm] = a.get("value", "")
                if afield:  # this is the question form (it has an answer input)
                    act = re.search(r'action="([^"]*)"', fm.group(0))
                    action = htmllib.unescape(act.group(1)) if act else str(r.url)
                    fields, answer_field = inputs, afield
                    break
            if not answer_field:
                log.warning("supersaas: question seen but no form/field: %s", text_of(r.text)[:300])
                return r
            if action.startswith("http"):
                target = action
            else:
                target = f"{BASE}{action}" if action.startswith("/") else f"{BASE}/{action}"
            log.info("supersaas: solved anti-bot question = %s", answer)
            r = await self.session.post(target, data=fields, headers={"referer": str(r.url)})
        return r

    async def _login(self) -> None:
        if self.session:
            await self.session.close()
        self.session = AsyncSession(proxies=self.proxies, timeout=30, impersonate="chrome")
        login_url = f"{BASE}/schedule/login/{ACCOUNT}/{SCHEDULE}"
        r = await self.session.get(login_url)
        r = await self._answer_challenge(r)
        form = FORM_RE.search(r.text)
        fields: dict[str, str] = {}
        action = login_url
        if form:
            action = htmllib.unescape(form.group(1))
            for raw in INPUT_RE.findall(form.group(2)):
                a = {k.lower(): htmllib.unescape(v) for k, v in ATTR_RE.findall(raw)}
                if a.get("name") and a.get("type", "text").lower() in ("hidden", "text", "email", "password", "checkbox"):
                    fields[a["name"]] = a.get("value", "")
        fields["name"] = self.email
        fields["password"] = self.password
        target = action if action.startswith("http") else f"{BASE}{action}"
        r = await self.session.post(target, data=fields, headers={"referer": login_url})
        r = await self._answer_challenge(r)   # a question may be shown right after login
        if "password" in text_of(r.text).lower() and "log in" in text_of(r.text).lower():
            log.warning("login failed, page: %s", text_of(r.text)[:500])
            raise Blocked("supersaas: login failed, check SUPERSAAS_EMAIL/PASSWORD")
        log.info("supersaas login ok")

    async def fetch(self) -> tuple[list[Slot], str]:
        try:
            return await self._fetch()
        except Blocked:
            raise
        except Exception as e:
            raise Blocked(f"supersaas network: {type(e).__name__}: {str(e)[:120]}") from e

    async def _fetch(self) -> tuple[list[Slot], str]:
        if not self.session:
            await self._login()
        url = f"{self.page_url()}?view=free"
        r = await self.session.get(url)
        r = await self._answer_challenge(r)
        if "/schedule/login/" in str(r.url) or "User Login" in r.text:
            await self._login()
            r = await self.session.get(url)
            r = await self._answer_challenge(r)
        if r.status_code != 200:
            log.warning("supersaas %s body: %s", r.status_code, text_of(r.text)[:400])
            raise Blocked(f"supersaas {r.status_code}")
        if not self._raw_logged:
            self._raw_logged = True
            log.info("free view sample: %s", text_of(r.text)[:1500])
        days = [] if no_free_slots(r.text) else parse_free_dates(r.text)
        earliest = days[0] if days else None
        log.info("supersaas: %d dates, earliest=%s", len(days), earliest)
        slot = Slot("grc", CENTRE, CATEGORY, earliest, earliest.isoformat() if earliest else "")
        return [slot], datetime.now().strftime("%Y-%m-%d %H:%M")

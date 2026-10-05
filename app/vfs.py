"""Client for VFS Global's public 'earliest available date' endpoint.

No login, no OTP. Cloudflare gates the endpoint on the client's TLS fingerprint, so we use
curl_cffi to impersonate Chrome. Datacenter IPs are usually refused: set PROXY_URL to a
residential/mobile proxy if every call comes back Blocked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from curl_cffi.requests import AsyncSession

log = logging.getLogger("vfs")

ENDPOINT = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot"
IMPERSONATE = ("chrome", "chrome124", "chrome120", "safari17_0")

SCHENGEN: dict[str, str] = {
    "aut": "Австрия", "bel": "Бельгия", "bgr": "Болгария", "che": "Швейцария", "cze": "Чехия",
    "deu": "Германия", "dnk": "Дания", "esp": "Испания", "est": "Эстония", "fin": "Финляндия",
    "fra": "Франция", "grc": "Греция", "hrv": "Хорватия", "hun": "Венгрия", "isl": "Исландия",
    "ita": "Италия", "lie": "Лихтенштейн", "ltu": "Литва", "lux": "Люксембург", "lva": "Латвия",
    "mlt": "Мальта", "nld": "Нидерланды", "nor": "Норвегия", "pol": "Польша", "prt": "Португалия",
    "rou": "Румыния", "svk": "Словакия", "svn": "Словения", "swe": "Швеция",
}

DATE_FORMATS = ("%d %b %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y")


class Blocked(Exception):
    """Cloudflare or VFS refused the request (bad IP / fingerprint)."""


class RateLimited(Blocked):
    """429: back off before asking again."""


class NotOffered(Exception):
    """VFS does not serve this mission in this country."""


@dataclass(frozen=True)
class Slot:
    mission: str
    centre: str
    category: str
    earliest: date | None
    raw_date: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.centre, self.category)


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    log.warning("unparsed date %r", raw)
    return None


def parse_response(mission: str, data: dict) -> list[Slot]:
    slots: list[Slot] = []
    for vac in data.get("vacList") or []:
        centre = (vac.get("vacName") or vac.get("vacCode") or "?").strip()
        for group in vac.get("visaGroupList") or []:
            raw = (group.get("earliestAvailableDate") or "").strip()
            slots.append(Slot(
                mission=mission,
                centre=centre,
                category=(group.get("displayName") or group.get("code") or "?").strip(),
                earliest=parse_date(raw),
                raw_date=raw,
            ))
    return slots


def _is_rate_limited(status: int, text: str) -> bool:
    return status == 429 or '"429' in text


class VfsPublicClient:
    def __init__(self, country: str, proxy_url: str | None):
        self.country = country
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.session = AsyncSession(proxies=proxies, timeout=30)
        self.working_profile: str | None = None
        self._raw_logged: set[str] = set()

    async def close(self) -> None:
        await self.session.close()

    def page_url(self, mission: str) -> str:
        return f"https://visa.vfsglobal.com/{self.country}/en/{mission}"

    async def fetch(self, mission: str) -> tuple[list[Slot], str]:
        """Returns (slots, VFS lastUpdatedOn). Raises NotOffered / RateLimited / Blocked."""
        payload = {"missionCode": mission, "countryCode": self.country, "cultureCode": "en-US"}
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://visa.vfsglobal.com",
            "referer": "https://visa.vfsglobal.com/",
            "route": f"{self.country}/en/{mission}",
        }
        profiles = (self.working_profile,) + tuple(p for p in IMPERSONATE if p != self.working_profile) \
            if self.working_profile else IMPERSONATE
        last = "no response"
        for profile in profiles:
            try:
                r = await self.session.post(ENDPOINT, json=payload, headers=headers, impersonate=profile)
            except Exception as e:  # network error, unsupported profile
                last = f"{type(e).__name__}: {e}"
                continue
            text = r.text or ""
            if _is_rate_limited(r.status_code, text):
                raise RateLimited(f"{r.status_code}: {text[:120]}")
            if r.status_code == 200 and text.lstrip().startswith("{"):
                data = r.json()
                self.working_profile = profile
                if mission not in self._raw_logged:
                    self._raw_logged.add(mission)
                    log.info("raw %s: %s", mission, text[:1500])
                if isinstance(data, dict) and data.get("vacList"):
                    return parse_response(mission, data), str(data.get("lastUpdatedOn") or "")
                raise NotOffered(text[:200])
            last = f"{r.status_code}: {text[:120]}"
            log.debug("profile %s rejected for %s: %s", profile, mission, last)
        raise Blocked(last)

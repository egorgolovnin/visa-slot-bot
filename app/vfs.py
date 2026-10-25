"""Client for VFS Global's public 'earliest available date' endpoint.

No login, no OTP. Cloudflare gates the endpoint on the client's TLS fingerprint, so we use
curl_cffi to impersonate Chrome. Datacenter IPs are usually refused: set PROXY_URL to a
residential/mobile proxy if every call comes back Blocked.
"""
from __future__ import annotations

import logging
import re
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

DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d.%m.%Y", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y")


class Blocked(Exception):
    """Cloudflare or VFS refused the request (bad IP / fingerprint)."""


class RateLimited(Blocked):
    """429: back off before asking again."""


class NotOffered(Exception):
    """VFS does not serve this mission in this country."""


class VfsEmpty(NotOffered):
    """200 OK with valid JSON but no vacList at all (totalVac null) — VFS returned an empty answer.
    For an already-offered country this is a transient VFS-side hiccup, not a real de-listing or a
    bug on our side, so the poller treats it gently rather than as a hard error."""


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


def data_age_minutes(data: dict) -> int | None:
    """VFS says how stale its answer is: lastUpdatedValue + lastUpdatedType (S/M/H/D)."""
    value, unit = data.get("lastUpdatedValue"), (data.get("lastUpdatedType") or "").upper()
    if not isinstance(value, (int, float)) or not unit:
        return None
    factor = {"S": 1 / 60, "M": 1, "H": 60, "D": 1440}.get(unit[0])
    return round(value * factor) if factor else None


_RL_CODE_RE = re.compile(r'"429\d{3}"')  # VFS rate-limit codes come as quoted strings: "429001" / "429002" …


def _is_rate_limited(status: int, text: str) -> bool:
    # Only HTTP 429, or a real VFS 429xxx code — not any stray '429' substring in a valid 200 body.
    return status == 429 or bool(_RL_CODE_RE.search(text[:300]))


class VfsPublicClient:
    def __init__(self, country: str, proxies=None):
        from .proxypool import ProxyPool
        self.country = country
        self.pool = ProxyPool(proxies)
        self.session = self._new_session()
        self.working_profile: str | None = None
        self._raw_logged: set[str] = set()
        self.last_age_min: int | None = None   # how old VFS's own cached data was, in minutes
        self.first_group_sample: str | None = None  # raw JSON of the first populated slot group ever seen

    def _new_session(self) -> AsyncSession:
        p = self.pool.current()
        return AsyncSession(proxies=({"http": p, "https": p} if p else None), timeout=30)

    async def _rotate(self) -> None:
        """A proxy flaked/banned: move to the next one and rebuild the session (no-op with one proxy)."""
        if self.pool.size > 1:
            self.pool.rotate()
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = self._new_session()

    async def close(self) -> None:
        await self.session.close()

    @staticmethod
    def split(code: str, default_country: str) -> tuple[str, str]:
        """'are-fra' -> ('are', 'fra'); plain 'hun' -> (default_country, 'hun')."""
        return tuple(code.split("-", 1)) if "-" in code else (default_country, code)

    def page_url(self, code: str) -> str:
        country, mission = self.split(code, self.country)
        return f"https://visa.vfsglobal.com/{country}/en/{mission}"

    async def fetch(self, code: str) -> tuple[list[Slot], str]:
        """Returns (slots, VFS lastUpdatedOn). Raises NotOffered / RateLimited / Blocked.
        `code` is a mission in the default country ('hun') or 'country-mission' ('are-fra')."""
        country, mission = self.split(code, self.country)
        payload = {"missionCode": mission, "countryCode": country, "cultureCode": "en-US"}
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://visa.vfsglobal.com",
            "referer": "https://visa.vfsglobal.com/",
            "route": f"{country}/en/{mission}",
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
                await self._rotate()  # this proxy is rate-limited: next call uses the next one
                raise RateLimited(f"{r.status_code}: {text[:120]}")
            if r.status_code == 200 and text.lstrip().startswith("{"):
                data = r.json()
                self.working_profile = profile
                if mission not in self._raw_logged:
                    self._raw_logged.add(mission)
                    log.info("raw %s: %s", mission, text[:1500])
                self.last_age_min = data_age_minutes(data) if isinstance(data, dict) else None
                if isinstance(data, dict) and data.get("vacList"):
                    # Capture the first populated group we ever see, so the poller can show us the
                    # real slot schema the moment a slot appears (confirms date field + format live).
                    if self.first_group_sample is None:
                        for vac in data["vacList"]:
                            grp = (vac.get("visaGroupList") or []) if isinstance(vac, dict) else []
                            if grp:
                                import json as _json
                                self.first_group_sample = _json.dumps(grp[0], ensure_ascii=False)[:600]
                                break
                    return parse_response(code, data), str(data.get("lastUpdatedOn") or "")
                # 200 + valid JSON but no vacList: VFS returned an empty/placeholder answer.
                raise VfsEmpty(text[:200])
            last = f"{r.status_code}: {text[:120]}"
            log.debug("profile %s rejected for %s: %s", profile, mission, last)
        await self._rotate()  # every profile failed on this proxy: next call uses the next one
        raise Blocked(last)

    async def raw_probe(self, code: str) -> str:
        """Diagnostics: hit the public earliest-slot endpoint and return the raw
        status + body + a few response headers, without raising. One line per profile."""
        country, mission = self.split(code, self.country)
        payload = {"missionCode": mission, "countryCode": country, "cultureCode": "en-US"}
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://visa.vfsglobal.com",
            "referer": "https://visa.vfsglobal.com/",
            "route": f"{country}/en/{mission}",
        }
        out = [f"route={country}/en/{mission}  payload={payload}"]
        for profile in IMPERSONATE:
            try:
                r = await self.session.post(ENDPOINT, json=payload, headers=headers, impersonate=profile)
            except Exception as e:
                out.append(f"{profile}: EXC {type(e).__name__}: {str(e)[:120]}")
                continue
            h = r.headers
            hint = " ".join(f"{k}={h.get(k)}" for k in ("cf-mitigated", "cf-ray", "content-type", "server") if h.get(k))
            out.append(f"{profile}: {r.status_code} {(r.text or '')[:160]}\n   {hint}")
            if r.status_code == 200:
                # Dump the JSON shape so we can confirm WHERE the availability lives and that
                # the parser reads the right fields (totalVac=1 is "1 center exists", not "1 slot").
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        out.append("  keys: " + ", ".join(list(data.keys())[:12]))
                        vac = data.get("vacList") or []
                        out.append(f"  vacList: {len(vac)} центр(ов)")
                        if vac and isinstance(vac[0], dict):
                            out.append("  vac[0] keys: " + ", ".join(vac[0].keys()))
                            grp = vac[0].get("visaGroupList") or []
                            out.append(f"  visaGroupList: {len(grp)}")
                            if grp and isinstance(grp[0], dict):
                                import json as _json
                                out.append("  group[0]: " + _json.dumps(grp[0], ensure_ascii=False)[:400])
                except Exception as e:
                    out.append(f"  (json parse failed: {type(e).__name__})")
                break
        return "\n".join(out)

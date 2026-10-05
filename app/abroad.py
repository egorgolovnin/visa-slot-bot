"""VFS centres outside Cyprus for clients who live elsewhere. Discovery keeps only centres whose public
'earliest slot' endpoint answers from our server for free (UAE, for one, needs a paid proxy)."""
from __future__ import annotations

import asyncio
import logging
import random

from .vfs import SCHENGEN, Blocked, NotOffered, RateLimited

log = logging.getLogger("abroad")

# ISO3 code used by VFS -> (button label, residence permit wording, genitive for "подача из …")
COUNTRIES = {
    "srb": ("🇷🇸 Сербия", "ВНЖ Сербии"), "mne": ("🇲🇪 Черногория", "ВНЖ Черногории"),
    "tur": ("🇹🇷 Турция", "ВНЖ Турции (икамет)"), "geo": ("🇬🇪 Грузия", "ВНЖ Грузии"),
    "arm": ("🇦🇲 Армения", "ВНЖ Армении"), "kaz": ("🇰🇿 Казахстан", "ВНЖ Казахстана"),
    "uzb": ("🇺🇿 Узбекистан", "ВНЖ Узбекистана"), "kgz": ("🇰🇬 Кыргызстан", "ВНЖ Кыргызстана"),
    "aze": ("🇦🇿 Азербайджан", "ВНЖ Азербайджана"), "isr": ("🇮🇱 Израиль", "ВНЖ Израиля"),
    "tha": ("🇹🇭 Таиланд", "визу/ВНЖ Таиланда"), "bih": ("🇧🇦 Босния и Герцеговина", "ВНЖ Боснии и Герцеговины"),
    "mkd": ("🇲🇰 Северная Македония", "ВНЖ Северной Македонии"), "alb": ("🇦🇱 Албания", "ВНЖ Албании"),
    "egy": ("🇪🇬 Египет", "ВНЖ Египта"), "idn": ("🇮🇩 Индонезия", "КИТАС Индонезии"),
    "are": ("🇦🇪 ОАЭ", "резидентская виза ОАЭ (Emirates ID)"),
}
DISABLED = {"are"}  # needs a paid residential proxy


def country_of(code: str) -> str | None:
    return code.split("-", 1)[0] if "-" in code else None


def mission_name(code: str) -> str:
    c, m = code.split("-", 1)
    return f"{SCHENGEN.get(m, m.upper())} ({COUNTRIES.get(c, (c,))[0].split(' ', 1)[-1]})"


async def discover(poller, countries: list[str] | None = None) -> str:
    """Probe every Schengen mission in each country; mark working centres as offered."""
    found: dict[str, list[str]] = {}
    blocked = []
    for c in DISABLED:  # switch these off right away, before the long probe
        for m in SCHENGEN:
            await poller.db.set_mission(f"{c}-{m}", mission_name(f"{c}-{m}"), False)
    for c in countries or [k for k in COUNTRIES if k not in DISABLED]:
        for m in SCHENGEN:
            code = f"{c}-{m}"
            for attempt in range(3):
                try:
                    await poller.client.fetch(code)
                    await poller.db.set_mission(code, mission_name(code), True)
                    found.setdefault(c, []).append(SCHENGEN[m])
                    break
                except NotOffered:
                    await poller.db.set_mission(code, mission_name(code), False)
                    break
                except RateLimited:
                    await asyncio.sleep(90)
                except Blocked as e:
                    log.info("abroad %s blocked: %s", code, str(e)[:80])
                    blocked.append(c)
                    break
            if c in blocked:
                break
            await asyncio.sleep(random.uniform(1.5, 3))
        log.info("abroad %s: %s", c, found.get(c, []))
    lines = [f"{COUNTRIES[c][0]}: {', '.join(v)}" for c, v in found.items()]
    return ("🌍 Визовые центры VFS за пределами Кипра, которые отвечают бесплатно:\n" + ("\n".join(lines) or "не найдено")
            + (f"\nНедоступны без прокси: {', '.join(COUNTRIES[c][0] for c in blocked)}" if blocked else ""))

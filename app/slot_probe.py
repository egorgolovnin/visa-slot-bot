"""Probe VFS slot endpoints to see, on real data, which one gives the fullest picture of free dates.

1) public  centerwithearliestslot  — no login, earliest date only
2) in-account CheckIsSlotAvailable  — needs a live session: per-centre/category availability
3) in-account calendar              — needs a live session: all free days in a month
Everything goes to the logs; a short summary is sent to admins.
"""
from __future__ import annotations

import html
import json
import logging

from .vfs_auth import LIFT, VfsSession, clientsource

log = logging.getLogger("slot_probe")


async def _post(sess: VfsSession, path: str, payload: dict) -> tuple[int, str]:
    """Authed lift-api POST. lift-api sits behind Cloudflare, so every call needs cf_clearance
    (via FlareSolverr through the same proxy) alongside the bearer token — same as login."""
    from . import cf
    ua = None
    clearance = await cf.clearance(sess.country, sess.mission, proxy=sess.proxy) if cf.enabled() else None
    if clearance:
        sess.cookies.update(clearance.get("cookies", {}))
        ua = clearance.get("user_agent")
    import re
    ua = ua or getattr(sess, "ua", None) or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    major = (re.search(r"Chrome/(\d+)", ua) or [None, "124"])[1]
    ch_ua = f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'
    async with sess._session() as s:
        if sess.cookies:
            s.cookies.update(sess.cookies)
        # Mirror the exact header set the login request uses so Cloudflare fingerprints us the same way.
        headers = {"accept": "application/json, text/plain, */*", "accept-language": "en-US,en;q=0.9",
                   "content-type": "application/json", "origin": "https://visa.vfsglobal.com",
                   "referer": f"https://visa.vfsglobal.com/{sess.country}/en/{sess.mission}/dashboard",
                   "route": f"{sess.country}/en/{sess.mission}", "clientsource": clientsource(),
                   "cfmlift": "mobile", "priority": "u=1, i", "user-agent": ua, "sec-ch-ua": ch_ua,
                   "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"', "sec-fetch-site": "same-site",
                   "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
                   "authorize": sess.token or "", "authorization": sess.token or ""}
        r = await s.post(f"{LIFT}/{path}", headers=headers, json=payload, impersonate=sess.impersonate)
        return r.status_code, (r.text or "")


async def probe_account(sess: VfsSession) -> str:
    """With a live Austria session, hit the in-account slot endpoints and log raw responses."""
    out = []
    # CheckIsSlotAvailable
    for path, payload in (
        ("appointment/CheckIsSlotAvailable",
         {"countryCode": sess.country, "missionCode": sess.mission, "loginUser": "", "payCode": "",
          "urn": "", "vacCode": "", "roleName": "Individual", "loadForFamily": False}),
        ("appointment/calendar",
         {"countryCode": sess.country, "missionCode": sess.mission, "centerCode": "", "visaCategoryCode": "",
          "visaSubCategoryCode": "", "payCode": "", "roleName": "Individual"}),
    ):
        try:
            status, text = await _post(sess, path, payload)
            log.info("SLOTPROBE %s status=%s len=%d", path, status, len(text))
            for i in range(0, min(len(text), 6000), 3000):
                log.info("SLOTPROBE %s part%d: %s", path, i // 3000, text[i:i + 3000])
            kind = "HTML" if text[:60].lstrip().lower().startswith(("<!doctype", "<html")) else "json/text"
            short = html.escape(text[:140].replace("\n", " "))
            out.append(f"{path.split('/')[-1]}: {status} · {kind} · {short}")
        except Exception as e:
            log.warning("SLOTPROBE %s failed: %s", path, e)
            out.append(f"{path.split('/')[-1]}: ошибка {str(e)[:60]}")
    return "🔬 In-account эндпоинты VFS (Австрия):\n" + "\n".join(out)

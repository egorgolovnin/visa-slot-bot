"""Probe VFS slot endpoints to see, on real data, which one gives the fullest picture of free dates.

1) public  centerwithearliestslot  — no login, earliest date only
2) in-account CheckIsSlotAvailable  — needs a live session: per-centre/category availability
3) in-account calendar              — needs a live session: all free days in a month
Everything goes to the logs; a short summary is sent to admins.
"""
from __future__ import annotations

import json
import logging

from .vfs_auth import LIFT, VfsSession, clientsource

log = logging.getLogger("slot_probe")


async def _post(sess: VfsSession, path: str, payload: dict) -> tuple[int, str]:
    async with sess._session() as s:
        if sess.cookies:
            s.cookies.update(sess.cookies)
        headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json",
                   "origin": "https://visa.vfsglobal.com", "referer": "https://visa.vfsglobal.com/",
                   "route": f"{sess.country}/en/{sess.mission}", "clientsource": clientsource(),
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
            short = text[:160].replace("\n", " ")
            out.append(f"{path.split('/')[-1]}: {status} · {short}")
        except Exception as e:
            log.warning("SLOTPROBE %s failed: %s", path, e)
            out.append(f"{path.split('/')[-1]}: ошибка {str(e)[:60]}")
    return "🔬 In-account эндпоинты VFS (Австрия):\n" + "\n".join(out)

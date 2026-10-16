"""Probe VFS slot endpoints to see, on real data, which one gives the fullest picture of free dates,
and WHY the in-account calls fail.

Key question: is the in-account 403102 a Cloudflare block (→ cf-mitigated header) or a VFS app-layer
rejection of a malformed request (→ clean JSON, fixable by sending the real centerCode/category)?
So we log the response HEADERS, and we call the endpoints BOTH empty and filled with the real
vacCode + visa category taken from the public endpoint.
"""
from __future__ import annotations

import html
import json
import logging

from curl_cffi.requests import AsyncSession

from .vfs_auth import LIFT, VfsSession, clientsource

log = logging.getLogger("slot_probe")

PUB = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot"
HDR_KEYS = ("cf-mitigated", "cf-ray", "server", "content-type", "x-amzn-requestid", "x-cache")


async def _post(sess: VfsSession, path: str, payload: dict) -> tuple[int, dict, str]:
    """Authed lift-api POST. Returns (status, response_headers, body)."""
    from . import cf
    import re
    ua = None
    clearance = await cf.clearance(sess.country, sess.mission, proxy=sess.proxy) if cf.enabled() else None
    if clearance:
        sess.cookies.update(clearance.get("cookies", {}))
        ua = clearance.get("user_agent")
    ua = ua or getattr(sess, "ua", None) or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    major = (re.search(r"Chrome/(\d+)", ua) or [None, "124"])[1]
    ch_ua = f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'
    async with sess._session() as s:
        if sess.cookies:
            s.cookies.update(sess.cookies)
        headers = {"accept": "application/json, text/plain, */*", "accept-language": "en-US,en;q=0.9",
                   "content-type": "application/json", "origin": "https://visa.vfsglobal.com",
                   "referer": f"https://visa.vfsglobal.com/{sess.country}/en/{sess.mission}/dashboard",
                   "route": f"{sess.country}/en/{sess.mission}", "clientsource": clientsource(),
                   "cfmlift": "mobile", "priority": "u=1, i", "user-agent": ua, "sec-ch-ua": ch_ua,
                   "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"', "sec-fetch-site": "same-site",
                   "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
                   "authorize": sess.token or "", "authorization": sess.token or ""}
        r = await s.post(f"{LIFT}/{path}", headers=headers, json=payload, impersonate=sess.impersonate)
        hdr = {k.lower(): v for k, v in dict(r.headers).items()}
        return r.status_code, hdr, (r.text or "")


async def _public_center(sess: VfsSession) -> tuple[str, str]:
    """Pull the real vacCode + visa category for this mission from the public endpoint."""
    proxies = {"http": sess.proxy, "https": sess.proxy} if sess.proxy else None
    headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json",
               "origin": "https://visa.vfsglobal.com", "referer": "https://visa.vfsglobal.com/",
               "route": f"{sess.country}/en/{sess.mission}"}
    payload = {"missionCode": sess.mission, "countryCode": sess.country, "cultureCode": "en-US"}
    async with AsyncSession(proxies=proxies, timeout=30) as ps:
        r = await ps.post(PUB, json=payload, headers=headers, impersonate="chrome")
        data = r.json() if r.status_code == 200 else {}
    vac = (data.get("vacList") or [{}])[0] if isinstance(data, dict) else {}
    center = vac.get("vacCode") or ""
    grp = vac.get("visaGroupList") or []
    category = (grp[0].get("code") or grp[0].get("visaGroup") or "") if grp else ""
    return center, category


def _verdict(status: int, hdr: dict, body: str) -> str:
    if hdr.get("cf-mitigated") or "challenge" in body.lower()[:200] or (body.lstrip().lower().startswith("<!doctype")):
        return "⛔ Cloudflare (cf-mitigated/challenge)"
    if status == 200:
        return "✅ 200"
    return f"⚠️ {status} — похоже на app-layer VFS (чистый JSON), не CF"


async def probe_account(sess: VfsSession) -> str:
    """Hit the in-account endpoints with empty AND filled payloads, log headers, and judge CF vs app-layer."""
    out = []
    try:
        center, category = await _public_center(sess)
        out.append(f"из публичного: vacCode=<code>{html.escape(center or '—')}</code> category=<code>{html.escape(category or '—')}</code>")
    except Exception as e:
        center, category = "", ""
        out.append(f"публичный центр не получен: {html.escape(str(e)[:80])}")

    variants = [
        ("CheckIsSlotAvailable·пусто", "appointment/CheckIsSlotAvailable",
         {"countryCode": sess.country, "missionCode": sess.mission, "loginUser": "", "payCode": "",
          "urn": "", "vacCode": "", "roleName": "Individual", "loadForFamily": False}),
        ("CheckIsSlotAvailable·сцентром", "appointment/CheckIsSlotAvailable",
         {"countryCode": sess.country, "missionCode": sess.mission, "loginUser": "", "payCode": "",
          "urn": "", "vacCode": center, "roleName": "Individual", "loadForFamily": False}),
        ("calendar·пусто", "appointment/calendar",
         {"countryCode": sess.country, "missionCode": sess.mission, "centerCode": "", "visaCategoryCode": "",
          "visaSubCategoryCode": "", "payCode": "", "roleName": "Individual"}),
        ("calendar·сцентром", "appointment/calendar",
         {"countryCode": sess.country, "missionCode": sess.mission, "centerCode": center,
          "visaCategoryCode": category, "visaSubCategoryCode": "", "payCode": "", "roleName": "Individual"}),
    ]
    for label, path, payload in variants:
        try:
            status, hdr, text = await _post(sess, path, payload)
            log.info("SLOTPROBE %s status=%s hdr=%s len=%d", label, status,
                     {k: hdr.get(k) for k in HDR_KEYS if hdr.get(k)}, len(text))
            for i in range(0, min(len(text), 6000), 3000):
                log.info("SLOTPROBE %s body%d: %s", label, i // 3000, text[i:i + 3000])
            hdr_line = " ".join(f"{k}={hdr.get(k)}" for k in HDR_KEYS if hdr.get(k))
            out.append(f"\n<b>{label}</b>: {_verdict(status, hdr, text)}\n"
                       f"   hdr: {html.escape(hdr_line)[:200]}\n"
                       f"   body: <code>{html.escape(text[:220].replace(chr(10), ' '))}</code>")
        except Exception as e:
            out.append(f"\n<b>{label}</b>: ошибка {html.escape(str(e)[:80])}")
    return "🔬 In-account VFS (" + sess.mission + "):\n" + "\n".join(out)

"""One-off probe of VFS booking-form configuration: GET /configuration/fields/{mission}/{country}.

This endpoint is public (the VFS web app calls it before login), so no account or captcha is needed.
The raw JSON goes to the logs; a short summary goes to admins.
"""
from __future__ import annotations

import json
import logging

from curl_cffi.requests import AsyncSession

log = logging.getLogger("vfs_fields")
BASE = "https://lift-api.vfsglobal.com/configuration/fields"


async def probe(country: str, missions: list[str], proxy: str | None) -> dict[str, object]:
    out: dict[str, object] = {}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    async with AsyncSession(proxies=proxies, timeout=30) as s:
        for m in missions:
            headers = {"accept": "application/json, text/plain, */*", "origin": "https://visa.vfsglobal.com",
                       "referer": "https://visa.vfsglobal.com/", "route": f"{country}/en/{m}"}
            try:
                r = await s.get(f"{BASE}/{m}/{country}", headers=headers, impersonate="chrome")
                text = r.text or ""
                log.info("FIELDS %s status=%s len=%d", m, r.status_code, len(text))
                for i in range(0, min(len(text), 12000), 3000):  # log in chunks so nothing is cut off
                    log.info("FIELDS %s part%d: %s", m, i // 3000, text[i:i + 3000])
                out[m] = json.loads(text) if text.lstrip().startswith(("{", "[")) else {"status": r.status_code}
            except Exception as e:
                log.warning("FIELDS %s failed: %s", m, e)
                out[m] = {"error": str(e)}
    return out


def summarize(data: dict[str, object]) -> str:
    lines = ["🔎 Поля формы записи VFS (без входа в аккаунт):"]
    for m, d in data.items():
        if isinstance(d, dict) and ("error" in d or "status" in d):
            lines.append(f"• {m}: не получилось ({d.get('error') or d.get('status')})")
            continue
        names = set()

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k.lower() in ("fieldname", "name", "controlname", "key", "label") and isinstance(v, str):
                        names.add(v)
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(d)
        lines.append(f"• {m}: {', '.join(sorted(names))[:600] or 'ответ получен, разбираю'}")
    return "\n".join(lines)

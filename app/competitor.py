"""One-off probe: find where cases.visarea.travel loads its open statistics from (the page is rendered by JS)."""
from __future__ import annotations

import logging
import re

from curl_cffi.requests import AsyncSession

log = logging.getLogger("competitor")
SITE = "https://cases.visarea.travel"


async def probe() -> None:
    async with AsyncSession(timeout=30, impersonate="chrome") as s:
        r = await s.get(SITE + "/")
        page = r.text or ""
        log.info("VISAREA page status=%s len=%d", r.status_code, len(page))
        # data embedded right in the page (Next.js/Nuxt/Astro style)
        for rx in (r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r'window\.__NUXT__\s*=\s*(.*?)</script>',
                   r'<script type="application/json"[^>]*>(.*?)</script>'):
            m = re.search(rx, page, re.S)
            if m:
                log.info("VISAREA embedded data (%s): %s", rx[:30], m.group(1)[:3000])
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', page)
        links = re.findall(r'(?:href|src)="([^"]+\.(?:json|csv))"', page)
        log.info("VISAREA scripts=%s data_links=%s", scripts, links)
        log.info("VISAREA html head: %s", re.sub(r"\s+", " ", page)[:2500])
        for src in scripts[:8]:
            url = src if src.startswith("http") else SITE + ("" if src.startswith("/") else "/") + src
            try:
                js = (await s.get(url)).text or ""
            except Exception as e:
                log.info("VISAREA script %s failed: %s", url, e)
                continue
            apis = sorted(set(re.findall(r'["\'`]((?:https?://[^"\'`\s]+)?/(?:api|data|cases|stats)[^"\'`\s]*)["\'`]', js)))
            jsons = sorted(set(re.findall(r'["\'`]([^"\'`\s]+\.json)["\'`]', js)))
            log.info("VISAREA script %s len=%d apis=%s jsons=%s", url, len(js), apis[:30], jsons[:30])
            for m in re.finditer(r'(consul|approv|одобр)', js[:2_000_000], re.I):
                log.info("VISAREA snippet: %s", js[max(0, m.start() - 300): m.start() + 500].replace("\n", " "))
                break
        for path in ("/api/cases", "/api/stats", "/data.json", "/cases.json", "/stats.json", "/api/data"):
            try:
                rr = await s.get(SITE + path)
                log.info("VISAREA try %s -> %s %s", path, rr.status_code, (rr.text or "")[:400].replace("\n", " "))
            except Exception as e:
                log.info("VISAREA try %s failed: %s", path, e)

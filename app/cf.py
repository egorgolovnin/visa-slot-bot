"""Get a Cloudflare clearance cookie for VFS via FlareSolverr, cache it, and reuse it in curl_cffi.

FlareSolverr runs a real browser, solves Cloudflare and returns cf_clearance + user-agent. We cache that
per (country, mission) and replay it from curl_cffi until it expires. Free, no proxy, no Capsolver.
FLARESOLVERR_URL points at the internal service, e.g. http://flaresolverr.railway.internal:8191
"""
from __future__ import annotations

import logging
import os
import time

import aiohttp

log = logging.getLogger("cf")
TTL = 25 * 60  # refresh well before Cloudflare's ~30 min
_cache: dict[str, dict] = {}


def enabled() -> bool:
    return bool(os.getenv("FLARESOLVERR_URL"))


async def clearance(country: str, mission: str, force: bool = False, proxy: str | None = None) -> dict | None:
    """Returns {'cookies': {...}, 'user_agent': str} for VFS, or None if FlareSolverr is unavailable.
    When a proxy is given, FlareSolverr solves Cloudflare THROUGH it, so cf_clearance matches the login IP."""
    proxy = proxy or os.getenv("PROXY_URL") or None
    key = f"{country}/{mission}"
    cached = _cache.get(key)
    if cached and not force and time.time() - cached["at"] < TTL:
        return cached["value"]
    base = os.getenv("FLARESOLVERR_URL", "").rstrip("/")
    if not base:
        return None
    url = f"https://visa.vfsglobal.com/{country}/en/{mission}/login"
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    if proxy:
        payload["proxy"] = {"url": proxy}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
            async with s.post(f"{base}/v1", json=payload) as r:
                data = await r.json(content_type=None)
    except Exception as e:
        log.warning("flaresolverr call failed: %s", e)
        return None
    sol = (data or {}).get("solution") or {}
    cookies = {c["name"]: c["value"] for c in sol.get("cookies", []) if "cf" in c["name"] or "cf_clearance" == c["name"]}
    if "cf_clearance" not in cookies:
        log.warning("no cf_clearance from flaresolverr: status=%s msg=%s", data.get("status"), data.get("message"))
        return None
    value = {"cookies": cookies, "user_agent": sol.get("userAgent", "")}
    _cache[key] = {"at": time.time(), "value": value}
    log.info("cf_clearance obtained for %s via flaresolverr", key)
    return value


def drop(country: str, mission: str) -> None:
    _cache.pop(f"{country}/{mission}", None)

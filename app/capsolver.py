"""Solve the Cloudflare Turnstile on the VFS login page via Capsolver.

VFS login has `enableCloudFlareCaptchaLogin: true`, so /user/login returns 403102 unless a valid
Turnstile token is sent as captcha_api_key. Capsolver returns that token; vfs_auth feeds it into the
normal HTTP login. Costs ~$0.001 per solve. Turn on with CAPSOLVER_KEY in the env.

Optional env:
  VFS_TURNSTILE_SITEKEY  — hard-set the sitekey (skip auto-discovery)
  CAPSOLVER_URL          — override API base (default https://api.capsolver.com)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import aiohttp

log = logging.getLogger("capsolver")
API = os.getenv("CAPSOLVER_URL", "https://api.capsolver.com").rstrip("/")
_sitekey_cache: dict[str, str] = {}


def enabled() -> bool:
    return bool(os.getenv("CAPSOLVER_KEY"))


def _login_url(country: str, mission: str) -> str:
    return f"https://visa.vfsglobal.com/{country}/en/{mission}/login"


async def sitekey(country: str, mission: str, proxy: str | None = None) -> str | None:
    """Turnstile sitekey for the VFS login page. Env override → cache → discover via FlareSolverr HTML."""
    env = os.getenv("VFS_TURNSTILE_SITEKEY")
    if env:
        return env.strip()
    key = f"{country}/{mission}"
    if key in _sitekey_cache:
        return _sitekey_cache[key]
    # discover from the rendered login page (FlareSolverr runs JS, so the widget is present)
    from . import cf
    html = await cf.rendered_html(country, mission, proxy=proxy) if hasattr(cf, "rendered_html") else None
    if html:
        m = re.search(r'data-sitekey=["\']([0-9a-zA-Z_\-]{10,})["\']', html) \
            or re.search(r'sitekey["\']?\s*[:=]\s*["\']([0-9a-zA-Z_\-]{10,})["\']', html)
        if m:
            _sitekey_cache[key] = m.group(1)
            log.info("discovered VFS turnstile sitekey: %s", m.group(1))
            return m.group(1)
    log.warning("turnstile sitekey not found — set VFS_TURNSTILE_SITEKEY in env (grab it from the login page DevTools)")
    return None


async def solve_turnstile(url: str, site_key: str, action: str | None = None,
                          cdata: str | None = None, timeout: int = 120) -> str | None:
    """Create a Turnstile task and poll for the token. Returns the token or None."""
    client_key = os.getenv("CAPSOLVER_KEY")
    if not client_key or not site_key:
        return None
    task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": url, "websiteKey": site_key}
    meta = {}
    if action:
        meta["action"] = action
    if cdata:
        meta["cdata"] = cdata
    if meta:
        task["metadata"] = meta
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout + 30)) as s:
        async with s.post(f"{API}/createTask", json={"clientKey": client_key, "task": task}) as r:
            j = await r.json()
        if j.get("errorId"):
            log.warning("capsolver createTask error: %s %s", j.get("errorCode"), j.get("errorDescription"))
            return None
        task_id = j.get("taskId")
        if not task_id:
            return None
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            async with s.post(f"{API}/getTaskResult",
                              json={"clientKey": client_key, "taskId": task_id}) as r:
                j = await r.json()
            if j.get("errorId"):
                log.warning("capsolver getTaskResult error: %s %s", j.get("errorCode"), j.get("errorDescription"))
                return None
            if j.get("status") == "ready":
                token = (j.get("solution") or {}).get("token")
                log.info("capsolver solved turnstile (%d chars)", len(token or ""))
                return token
        log.warning("capsolver timed out after %ss", timeout)
        return None


async def balance() -> float | None:
    """Account balance in USD, for a quick health check."""
    client_key = os.getenv("CAPSOLVER_KEY")
    if not client_key:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(f"{API}/getBalance", json={"clientKey": client_key}) as r:
                j = await r.json()
        return float(j.get("balance")) if j.get("balance") is not None else None
    except Exception as e:
        log.warning("capsolver balance check failed: %s", e)
        return None

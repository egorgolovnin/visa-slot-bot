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


# Turnstile sitekeys always start with this prefix — a very reliable anchor in HTML or JS bundles.
_SK_RE = re.compile(r'0x4AAAAAAA[A-Za-z0-9_\-]{6,}')


async def _fetch_text(url: str, proxy: str | None) -> str | None:
    """GET a static asset (JS bundle) through the proxy with a browser-ish TLS profile."""
    from curl_cffi.requests import AsyncSession
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies, timeout=40) as s:
            r = await s.get(url, headers={"accept": "*/*", "referer": "https://visa.vfsglobal.com/"})
            return r.text or ""
    except Exception as e:
        log.info("fetch bundle failed %s: %s", url, e)
        return None


async def sitekey(country: str, mission: str, proxy: str | None = None) -> str | None:
    """Turnstile sitekey for the VFS login page. Env override → cache → rendered HTML → JS bundles."""
    env = os.getenv("VFS_TURNSTILE_SITEKEY")
    if env:
        return env.strip()
    key = f"{country}/{mission}"
    if key in _sitekey_cache:
        return _sitekey_cache[key]
    from . import cf
    html = await cf.rendered_html(country, mission, proxy=proxy) if hasattr(cf, "rendered_html") else None

    def _remember(sk: str) -> str:
        _sitekey_cache[key] = sk
        log.info("discovered VFS turnstile sitekey: %s", sk)
        return sk

    if html:
        m = _SK_RE.search(html) \
            or re.search(r'data-sitekey=["\']([0-9a-zA-Z_\-]{10,})["\']', html) \
            or re.search(r'sitekey["\']?\s*[:=]\s*["\']([0-9a-zA-Z_\-]{10,})["\']', html)
        if m:
            return _remember(m.group(0) if m.re is _SK_RE else m.group(1))
        # not in the DOM — scan the Angular JS bundles the page loads
        base = "https://visa.vfsglobal.com"
        srcs = re.findall(r'<script[^>]+src=["\']([^"\']+\.js)["\']', html)
        # prefer main/runtime bundles, cap how many we fetch
        srcs = sorted(set(srcs), key=lambda s: (0 if re.search(r'main|runtime|scripts', s) else 1))[:8]
        for src in srcs:
            url = src if src.startswith("http") else base + ("" if src.startswith("/") else "/") + src.lstrip("/")
            js = await _fetch_text(url, proxy)
            if js:
                m = _SK_RE.search(js)
                if m:
                    return _remember(m.group(0))
    # last resort: load the page in Camoufox and read the sitekey from turnstile.render / DOM
    try:
        from . import vfs_browser
        sk = await vfs_browser.sitekey_via_browser(country, mission, proxy)
        if sk:
            return _remember(sk)
    except Exception as e:
        log.info("browser sitekey discovery failed: %s", e)
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


async def diagnose(country: str, mission: str, proxy: str | None = None) -> str:
    """Human-readable check: key present? API reachable? balance? sitekey found?"""
    lines = [f"CAPSOLVER_KEY: {'задан' if enabled() else 'НЕ задан'}"]
    if enabled():
        client_key = os.getenv("CAPSOLVER_KEY")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.post(f"{API}/getBalance", json={"clientKey": client_key}) as r:
                    j = await r.json(content_type=None)
            if j.get("errorId"):
                lines.append(f"API: ошибка {j.get('errorCode')} — {j.get('errorDescription')}")
            else:
                lines.append(f"API: ok, баланс ${j.get('balance')}")
        except Exception as e:
            lines.append(f"API: НЕ достучался ({str(e)[:120]})")
    sk = await sitekey(country, mission, proxy)
    lines.append(f"sitekey: {sk or 'НЕ найден'}")
    return "\n".join(lines)


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

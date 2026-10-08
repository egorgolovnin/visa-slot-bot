"""Log in to VFS with Camoufox (patched Firefox) which passes Cloudflare Turnstile at the engine level.

Camoufox opens the VFS login page, solves Turnstile, fills email+password in the real form and submits.
On success the app stores an access token; we hand the browser's cookies + token back for API booking.
Sessions are persisted so we log in rarely (token ~6h). Needs the Camoufox Firefox binary (fetched on build).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("vfs_browser")
STATE_DIR = Path(os.getenv("BROWSER_STATE_DIR", "/app/state"))
LOGIN_URL = "https://visa.vfsglobal.com/{country}/en/{mission}/login"


def available() -> bool:
    try:
        import camoufox  # noqa: F401
        return True
    except Exception:
        return False


async def login(country: str, mission: str, email: str, password: str, proxy: str | None = None,
                timeout: int = 90) -> dict | None:
    """Returns {'token': str, 'cookies': {...}, 'user_agent': str} or None. Runs the whole browser login."""
    from camoufox.async_api import AsyncCamoufox

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"vfs_{country}_{mission}.json"
    proxy_cfg = None
    if proxy:
        # proxy like http://user:pass@host:port
        import re
        m = re.match(r"https?://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)", proxy)
        if m:
            u, pw, host, port = m.groups()
            proxy_cfg = {"server": f"http://{host}:{port}"}
            if u:
                proxy_cfg |= {"username": u, "password": pw}

    token = None
    result = None
    launch = dict(headless=True, os="windows", humanize=True, proxy=proxy_cfg)
    try:
        import camoufox.locale  # geoip extra present?
        launch["geoip"] = bool(proxy)
    except Exception:
        pass
    async with AsyncCamoufox(**launch) as browser:
        ctx_args = {}
        if state_file.exists():
            ctx_args["storage_state"] = str(state_file)
        page = await browser.new_page()

        # capture the login API response to grab the access token
        async def on_response(resp):
            nonlocal token, result
            if "/user/login" in resp.url:
                try:
                    data = await resp.json()
                    if data.get("accessToken"):
                        token = data["accessToken"]
                        result = data
                except Exception:
                    pass
        page.on("response", on_response)

        log.info("vfs_browser: goto login")
        await page.goto(LOGIN_URL.format(country=country, mission=mission), wait_until="networkidle", timeout=timeout * 1000)
        await _pass_turnstile(page)
        # Angular renders the form via JS after load — wait for an actual email input to exist
        EMAIL = ("input#email, input[type=email], input[formcontrolname=email], "
                 "input[name=email], input[autocomplete=username], input[placeholder*=mail i]")
        PWD = ("input#password, input[type=password], input[formcontrolname=password], "
               "input[name=password], input[autocomplete=current-password]")
        btn = ("button[type=submit], button:has-text('Sign In'), button:has-text('Sign in'), "
               "button:has-text('Login'), button:has-text('Log in'), button:has-text('Submit')")
        try:
            await asyncio.sleep(6)
            target = None  # the page or frame that actually holds the email field
            for attempt in range(5):
                for fr in [page] + page.frames:
                    try:
                        el = await fr.query_selector(EMAIL)
                        if el:
                            target = fr
                            break
                    except Exception:
                        continue
                if target:
                    break
                # log what each frame has, to diagnose
                for fr in page.frames:
                    try:
                        n = len(await fr.query_selector_all("input"))
                        if n:
                            log.info("vfs_browser: frame %s has %d inputs", (fr.url or "")[:60], n)
                    except Exception:
                        pass
                await _pass_turnstile(page)
                await asyncio.sleep(3)
            if not target:
                raise RuntimeError("email field not found in any frame")
            await target.fill(EMAIL, email, timeout=15000)
            await target.fill(PWD, password, timeout=15000)
            log.info("vfs_browser: filled form in frame %s", (target.url or "main")[:60])
            await _pass_turnstile(page)
            await asyncio.sleep(1)
            try:
                await target.click(btn, timeout=10000)
            except Exception:
                await page.click(btn, timeout=10000)
            log.info("vfs_browser: submitted")
        except Exception as e:
            frames_info = []
            for fr in page.frames:
                try:
                    frames_info.append(f"{(fr.url or '')[:40]}:{len(await fr.query_selector_all('input'))}")
                except Exception:
                    pass
            log.warning("form fill failed: %s | frames=%s", e, frames_info)
        # wait for the token to arrive
        for _ in range(timeout):
            if token:
                break
            await asyncio.sleep(1)
        cookies = {c["name"]: c["value"] for c in await page.context.cookies()}
        ua = await page.evaluate("navigator.userAgent")
        if token:
            await page.context.storage_state(path=str(state_file))
            log.info("VFS browser login ok %s-%s", country, mission)
            return {"token": token, "cookies": cookies, "user_agent": ua}
        try:
            body = await page.content()
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / "last_login.html").write_text(body[:200000], encoding="utf-8")
            await page.screenshot(path=str(STATE_DIR / "last_login.png"))
        except Exception:
            body = ""
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:400]
        log.warning("VFS browser login: no token. visible text: %s", snippet)
        return None


async def _pass_turnstile(page) -> None:
    """Give Camoufox's engine time to clear a Turnstile challenge if one is shown."""
    for _ in range(20):
        try:
            if await page.query_selector("iframe[src*='challenges.cloudflare.com']") or \
               await page.query_selector(".cf-turnstile"):
                await asyncio.sleep(2)
                continue
        except Exception:
            pass
        break

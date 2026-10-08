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


STEPS: list[str] = []  # last login attempt's step trace, for reporting to the admin


async def login(country: str, mission: str, email: str, password: str, proxy: str | None = None,
                timeout: int = 90) -> dict | None:
    """Returns {'token': str, 'cookies': {...}, 'user_agent': str} or None. Runs the whole browser login."""
    from camoufox.async_api import AsyncCamoufox
    STEPS.clear()
    def step(m): STEPS.append(m); log.info("vfs_browser STEP: %s", m)

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
    import os as _os
    headless = _os.getenv("BROWSER_HEADLESS", "virtual")  # "virtual" => xvfb non-headless, "true"/"false" override
    hl = True if headless == "true" else (False if headless == "false" else "virtual")
    launch = dict(headless=hl, os="windows", humanize=True, proxy=proxy_cfg)
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
        login_seen = {"status": None, "body": ""}

        async def on_response(resp):
            nonlocal token, result
            if "/user/login" in resp.url:
                try:
                    body = await resp.text()
                    login_seen["status"] = resp.status
                    login_seen["body"] = body[:200]
                    log.info("vfs_browser: /user/login -> %s %s", resp.status, body[:150])
                    STEPS.append(f"ответ входа: {resp.status} {body[:80]}")
                    data = __import__("json").loads(body)
                    if data.get("accessToken"):
                        token = data["accessToken"]
                        result = data
                except Exception as ex:
                    log.info("vfs_browser: login resp parse: %s", ex)
        page.on("response", on_response)

        log.info("vfs_browser: goto login")
        await page.goto(LOGIN_URL.format(country=country, mission=mission), wait_until="networkidle", timeout=timeout * 1000)
        # accept/close the OneTrust cookie banner so it doesn't intercept clicks
        for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept All Cookies')",
                    "button:has-text('Accept Only Necessary')"):
            try:
                b = await page.query_selector(sel)
                if b:
                    await b.click(timeout=3000)
                    log.info("vfs_browser: cookie banner dismissed")
                    break
            except Exception:
                pass
        await _pass_turnstile(page)
        # Angular renders the form via JS after load — wait for an actual email input to exist
        EMAIL = ("input#email, input[type=email], input[formcontrolname=email], "
                 "input[name=email], input[autocomplete=username], input[placeholder*=mail i]")
        PWD = ("input#password, input[type=password], input[formcontrolname=password], "
               "input[name=password], input[autocomplete=current-password]")
        btn = ("button[type=submit], button:has-text('Sign In'), button:has-text('Sign in'), "
               "button:has-text('Login'), button:has-text('Log in'), button:has-text('Submit')")
        filled = False
        try:
            for attempt in range(6):
                await _pass_turnstile(page)
                res = await page.evaluate(
                    """([em, pw]) => {
                        const vis = el => el.offsetParent !== null && !el.disabled && !el.readOnly;
                        const ins = [...document.querySelectorAll('input')].filter(vis);
                        const pass = ins.find(e => e.type === 'password');
                        const mail = ins.find(e => e.type === 'email')
                            || ins.find(e => /mail|user|login/i.test(e.name + e.id + (e.getAttribute('formcontrolname')||'') + (e.placeholder||'') + (e.getAttribute('autocomplete')||'')))
                            || ins.find(e => e !== pass && (e.type === 'text' || !e.type));
                        const set = (el, v) => { if(!el) return false;
                            const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                            d.call(el, v);
                            el.dispatchEvent(new Event('input',{bubbles:true}));
                            el.dispatchEvent(new Event('change',{bubbles:true}));
                            el.dispatchEvent(new Event('blur',{bubbles:true}));
                            return true; };
                        return {inputs: ins.length, mail: set(mail, em), pass: set(pass, pw),
                                mailInfo: mail ? (mail.type+'/'+(mail.getAttribute('formcontrolname')||mail.name||mail.id||mail.placeholder)) : null};
                    }""", [email, password])
                log.info("vfs_browser fill try %d: %s", attempt, res)
                if res.get("mail") and res.get("pass"):
                    filled = True
                    step(f"поля найдены ({res.get('mailInfo')})")
                    break
                await asyncio.sleep(3)
            if not filled:
                raise RuntimeError("email/password inputs not found")
            # tag fields, then clear + type with the keyboard so Angular Material marks them valid
            try:
                await page.evaluate("""() => {
                    const vis = el => el.offsetParent !== null && !el.disabled;
                    const ins = [...document.querySelectorAll('input')].filter(vis);
                    const pass = ins.find(e => e.type === 'password');
                    const mail = ins.find(e => e !== pass);
                    if (mail){ mail.setAttribute('data-vfsmail','1'); mail.value=''; }
                    if (pass){ pass.setAttribute('data-vfspass','1'); pass.value=''; }
                }""")
                me = await page.query_selector("input[data-vfsmail]")
                pe = await page.query_selector("input[data-vfspass]")
                if me:
                    await me.click(timeout=4000, force=True)
                    await page.keyboard.type(email, delay=50)
                if pe:
                    await pe.click(timeout=4000, force=True)
                    await page.keyboard.type(password, delay=50)
                await page.keyboard.press("Tab")
                step("логин/пароль введены")
            except Exception as e:
                log.info("vfs_browser: keyboard typing failed: %s", e)
            # click the Turnstile checkbox, then WAIT for it to solve (button stays disabled until then)
            await _click_turnstile(page)
            solved = await _pass_turnstile(page, timeout=70)
            if not solved:
                await _click_turnstile(page)
                solved = await _pass_turnstile(page, timeout=40)
            step(f"Turnstile {'пройден' if solved else 'НЕ пройден'}")
            # wait for the button to become enabled (Angular enables it once form+turnstile are valid)
            for _ in range(15):
                st = await page.evaluate("""() => {
                    const b = [...document.querySelectorAll('button')].find(x => /sign ?in/i.test(x.textContent));
                    return b ? b.disabled : null;
                }""")
                if st is False:
                    break
                await asyncio.sleep(1)
            btnstate = await page.evaluate("""() => {
                const b = [...document.querySelectorAll('button')].find(x =>
                    /sign ?in|log ?in|login|submit/i.test(x.textContent));
                return b ? {found:true, disabled:b.disabled, text:b.textContent.trim().slice(0,20)} : {found:false};
            }""")
            step(f"кнопка Sign In: {'активна' if btnstate.get('disabled') is False else 'заблокирована'}")
            try:
                await page.screenshot(path=str(STATE_DIR / "step_filled.png"))
            except Exception:
                pass
            # click the submit button like a real user (Angular listens on a real click / form submit)
            clicked = False
            for sel in ("button:has-text('Sign In')", "button:has-text('Sign in')", "button:has-text('Login')",
                        "button:has-text('Log in')", "button[type=submit]"):
                try:
                    b = await page.query_selector(sel)
                    if b:
                        try:
                            await b.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        await b.click(timeout=5000, force=True)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # fallback: submit the form or press Enter in the password field
                await page.keyboard.press("Enter")
            step(f"Sign In нажата (clicked={clicked})")
            # give the login request time to fly
            for _ in range(20):
                if token or login_seen["status"]:
                    break
                await asyncio.sleep(1)
            try:
                await page.screenshot(path=str(STATE_DIR / "step_after_submit.png"))
                txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", await page.content()))
                for kw in ("invalid", "incorrect", "required", "error", "otp", "verification", "captcha", "wrong"):
                    if kw in txt.lower():
                        i = txt.lower().index(kw)
                        log.info("vfs_browser: page mentions '%s': ...%s...", kw, txt[max(0,i-40):i+60])
            except Exception:
                pass
        except Exception as e:
            try:
                (STATE_DIR / "last_login.html").write_text((await page.content())[:200000], encoding="utf-8")
                await page.screenshot(path=str(STATE_DIR / "last_login.png"))
            except Exception:
                pass
            log.warning("form fill failed: %s", e)

        # wait for the token to arrive
        for _ in range(timeout):
            if token:
                break
            await asyncio.sleep(1)
        cookies = {c["name"]: c["value"] for c in await page.context.cookies()}
        ua = await page.evaluate("navigator.userAgent")
        if token:
            await page.context.storage_state(path=str(state_file))
            step("✅ токен получен")
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


async def _click_turnstile(page) -> None:
    """Click the visible Turnstile checkbox inside its iframe (Camoufox doesn't always auto-click)."""
    for _ in range(3):
        try:
            for fr in page.frames:
                if "challenges.cloudflare.com" in (fr.url or ""):
                    for sel in ("input[type=checkbox]", "label", ".cb-c", "#challenge-stage"):
                        try:
                            el = await fr.query_selector(sel)
                            if el:
                                await el.click(timeout=3000)
                                log.info("vfs_browser: clicked turnstile checkbox")
                                return
                        except Exception:
                            continue
            # fallback: click the widget box on the main page by coordinates
            box = await page.query_selector(".cf-turnstile, [data-sitekey]")
            if box:
                bb = await box.bounding_box()
                if bb:
                    await page.mouse.click(bb["x"] + 28, bb["y"] + bb["height"] / 2)
                    log.info("vfs_browser: clicked turnstile by coords")
                    return
        except Exception as e:
            log.info("vfs_browser: turnstile click: %s", e)
        await asyncio.sleep(2)


async def _pass_turnstile(page, timeout: int = 60) -> bool:
    """Wait until Cloudflare Turnstile is solved (its hidden response token gets a value). Returns True if solved."""
    for _ in range(timeout):
        try:
            tok = await page.evaluate("""() => {
                const t = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
                return t ? (t.value || '') : '__notok__';
            }""")
            if tok and tok != "__notok__":
                return True          # token present -> solved
            if tok == "__notok__":
                # no turnstile widget on the page at all
                has = await page.query_selector("iframe[src*='challenges.cloudflare.com'], .cf-turnstile")
                if not has:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False

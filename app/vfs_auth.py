"""Log in to a VFS account from the server and keep a live session (access token ~6h).

Austria (aut) needs no captcha at login, so its session can be refreshed with no Capsolver; a code may arrive
by email and is read from the service mailbox. Other missions need a Turnstile token (Capsolver) — added later.
Password and clientsource are RSA-encrypted with VFS's public key, exactly as the web app does.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from curl_cffi.requests import AsyncSession

log = logging.getLogger("vfs_auth")
LIFT = "https://lift-api.vfsglobal.com"
PEM = (Path(__file__).with_name("vfs_pubkey.pem")).read_bytes()
TOKEN_TTL = 5.5 * 3600
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


class LoginError(Exception):
    pass


class OtpRequired(Exception):
    """VFS asked for an email OTP; the caller retries login with the code."""


def rsa_encrypt(plain: bytes) -> str:
    pub = load_pem_public_key(PEM)
    ct = pub.encrypt(plain, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(ct).decode()


def clientsource(prefix: str = "GA") -> str:
    ts = datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%dT%H:%M:%S")
    return rsa_encrypt(f"{prefix};{ts}Z".encode())


def accounts() -> list[tuple[str, str]]:
    """VFS_ACCOUNTS = 'email:password,email2:password2'."""
    out = []
    for item in os.getenv("VFS_ACCOUNTS", "").split(","):
        if ":" in item:
            email, pwd = item.split(":", 1)
            out.append((email.strip(), pwd.strip()))
    return out


class VfsSession:
    def __init__(self, country: str, mission: str, proxy: str | None):
        self.country, self.mission, self.proxy = country, mission, proxy
        self.token: str | None = None
        self.obtained = 0.0
        self.cookies: dict = {}
        self.impersonate = "chrome124"

    @property
    def alive(self) -> bool:
        return bool(self.token) and (time.time() - self.obtained) < TOKEN_TTL

    def _session(self) -> AsyncSession:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        return AsyncSession(impersonate=self.impersonate, proxies=proxies, timeout=40)

    async def login(self, email: str, password: str, turnstile: str | None = None, otp: str | None = None) -> None:
        base = {"accept-language": "en-US,en;q=0.9", "origin": "https://visa.vfsglobal.com",
                "referer": "https://visa.vfsglobal.com/", "route": f"{self.country}/en/{self.mission}",
                "user-agent": UA}
        async with self._session() as s:
            try:
                await s.get(f"https://visa.vfsglobal.com/{self.country}/en/{self.mission}/login",
                            headers={"accept": "text/html", "user-agent": UA})
                await s.get(f"{LIFT}/configuration/fields/{self.mission}/{self.country}",
                            headers={**base, "accept": "application/json", "clientsource": clientsource()})
            except Exception as e:
                log.info("warmup failed: %s", e)
            payload = {"username": email, "password": rsa_encrypt(password.encode()),
                       "missioncode": self.mission, "countrycode": self.country, "languageCode": "en-US"}
            if turnstile:
                payload |= {"captcha_version": "cloudflare-v1", "captcha_api_key": turnstile}
            if otp:
                payload["otp"] = otp
            headers = {**base, "accept": "application/json, text/plain, */*", "clientsource": clientsource(),
                       "cfmlift": "mobile", "content-type": "application/json"}
            r = await s.post(f"{LIFT}/user/login", headers=headers, json=payload)
            text = r.text or ""
            if r.status_code == 403 and "Just a moment" in text[:200]:
                raise LoginError("cloudflare block at login (need Turnstile / proxy)")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise LoginError(f"non-JSON login response: {text[:150]}")
            if r.status_code == 429 or str(data.get("code")) in ("429001", "429201"):
                raise LoginError(f"rate limited: {data.get('code')}")
            if re.search(r"otp|verification code|one.time", json.dumps(data).lower()) and not data.get("accessToken"):
                raise OtpRequired()
            err = data.get("error")
            if isinstance(err, dict) and err.get("code"):
                raise LoginError(f"auth fail {err['code']}: {err.get('description', '')}")
            if isinstance(err, str) and err:
                raise LoginError(f"auth fail: {err[:120]}")
            if data.get("accessToken"):
                self.token = data["accessToken"]
                self.obtained = time.time()
                self.cookies = dict(r.cookies)
                log.info("VFS %s-%s login ok, remaining=%s", self.country, self.mission, data.get("remainingCount"))
                return
            raise LoginError(f"unexpected login response: {text[:150]}")


async def login_austria(mailbox_wait, proxy: str | None = None) -> VfsSession | None:
    """Austria: no captcha. Returns a live session or None. `mailbox_wait` reads an OTP if VFS asks for one."""
    accs = accounts()
    if not accs:
        log.info("no VFS_ACCOUNTS configured")
        return None
    email, pwd = accs[0]
    sess = VfsSession("cyp", "aut", proxy)
    try:
        await sess.login(email, pwd)
    except OtpRequired:
        since = datetime.now(timezone.utc) - timedelta(minutes=2)
        code = await mailbox_wait(sender_like="vfs", pattern=r"\b(\d{4,8})\b", since=since, timeout=180)
        if not code:
            raise LoginError("OTP did not arrive by email")
        await sess.login(email, pwd, otp=code)
    return sess if sess.alive else None

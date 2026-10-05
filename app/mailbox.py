"""Service mailbox (Gmail over IMAP with an app password).

Used by automated booking: VFS login/booking OTP codes and the German embassy's
"confirm your appointment" link arrive here. Credentials: SERVICE_EMAIL, SERVICE_EMAIL_PASSWORD.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

log = logging.getLogger("mailbox")
HOST = os.getenv("SERVICE_EMAIL_IMAP", "imap.gmail.com")


def enabled() -> bool:
    return bool(os.getenv("SERVICE_EMAIL") and os.getenv("SERVICE_EMAIL_PASSWORD"))


def address() -> str:
    return os.getenv("SERVICE_EMAIL", "")


def _connect() -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(HOST, 993, timeout=30)
    m.login(os.environ["SERVICE_EMAIL"], os.environ["SERVICE_EMAIL_PASSWORD"].replace(" ", ""))
    return m


def _text(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def _check_sync() -> int:
    m = _connect()
    try:
        status, data = m.select("INBOX", readonly=True)
        return int(data[0]) if status == "OK" else -1
    finally:
        m.logout()


async def check() -> int:
    """Self-test: logs in and returns the number of messages in INBOX."""
    return await asyncio.to_thread(_check_sync)


def _recent_sync(since: datetime, sender_like: str | None, subject_like: str | None) -> list[dict]:
    m = _connect()
    out = []
    try:
        m.select("INBOX", readonly=True)
        query = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
        status, ids = m.search(None, query)
        for num in reversed(ids[0].split()[-50:]):
            status, raw = m.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
            frm = str(make_header(decode_header(msg.get("From", ""))))
            subj = str(make_header(decode_header(msg.get("Subject", ""))))
            try:
                when = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                when = datetime.now(timezone.utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < since:
                continue
            if sender_like and sender_like.lower() not in frm.lower():
                continue
            if subject_like and subject_like.lower() not in subj.lower():
                continue
            out.append({"from": frm, "subject": subj, "date": when, "body": _text(msg)})
    finally:
        m.logout()
    return out


async def wait_for(sender_like: str | None = None, subject_like: str | None = None, pattern: str | None = None,
                   since: datetime | None = None, timeout: int = 180, every: int = 6) -> str | None:
    """Poll the inbox until a matching message arrives; return the first regex group of `pattern`
    (e.g. an OTP code or a confirmation link), or the body if no pattern is given."""
    since = since or datetime.now(timezone.utc) - timedelta(minutes=2)
    deadline = asyncio.get_running_loop().time() + timeout
    rx = re.compile(pattern, re.S | re.I) if pattern else None
    while asyncio.get_running_loop().time() < deadline:
        try:
            for msg in await asyncio.to_thread(_recent_sync, since, sender_like, subject_like):
                if not rx:
                    return msg["body"]
                found = rx.search(msg["body"]) or rx.search(msg["subject"])
                if found:
                    return found.group(1) if found.groups() else found.group(0)
        except Exception as e:
            log.warning("mailbox poll failed: %s", e)
        await asyncio.sleep(every)
    return None

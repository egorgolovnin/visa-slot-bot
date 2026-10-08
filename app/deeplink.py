"""Parsing of /start payloads coming from t.me deep links.

Formats (Telegram allows A-Z a-z 0-9 _ - and up to 64 chars):
  ref<tg_id>            referral link of a user (handled separately in bot.start)
  <action>              open a screen right away: wheel / invite / order
  <action>-<tag>        same, plus a traffic-source tag, e.g. wheel-site_home
  <tag>                 plain source tag, e.g. community (old /link format)
"""
from __future__ import annotations

import re

ACTIONS = ("wheel", "invite", "order")
_CLEAN = re.compile(r"[^\w\-]")


def parse_start_payload(payload: str | None) -> tuple[str | None, str]:
    """Return (action, source_tag). action is None for a plain tag or empty payload."""
    p = _CLEAN.sub("", (payload or "").strip())[:64]
    if not p:
        return None, ""
    head, _, tail = p.partition("-")
    if head in ACTIONS:
        return head, (tail or head)
    return None, p

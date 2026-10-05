"""Owner's dashboard: a Telegram Mini App (opened from /admin) plus a key-protected link for desktop.

Auth: either Telegram WebApp initData signed with the bot token and belonging to an admin,
or ?key=<PANEL_KEY>. The page itself is static; all numbers come from /api/panel.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from .config import Settings
from .db import Database

log = logging.getLogger("web")
PAGE = Path(__file__).with_name("panel.html")


def public_url() -> str | None:
    domain = os.getenv("PUBLIC_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else None


def panel_key() -> str | None:
    return os.getenv("PANEL_KEY") or None


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict | None:
    """Telegram Mini App signature check. Returns the user dict if valid."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > max_age:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None


def _authorized(request: web.Request, settings: Settings) -> bool:
    key = panel_key()
    if key and hmac.compare_digest(request.headers.get("X-Panel-Key", ""), key):
        return True
    user = verify_init_data(request.headers.get("X-Init-Data", ""), settings.bot_token)
    return bool(user) and int(user.get("id", 0)) in settings.admin_ids


def _num(v):
    return float(v) if v is not None else 0.0


async def collect(db: Database, settings: Settings, poller) -> dict:
    from .orders import DESTINATIONS, STATUS
    from .slotstats import compute

    u = await db.user_summary()
    o = await db.orders_summary()
    days = await db.daily_new_users(30)
    sources = await db.top_sources(10)
    recent = await db.orders_list(tuple(STATUS), limit=40)
    payments = await db.pool.fetch(
        """SELECT id, destination, mission, price_eur, paid_at, paid_asset, paid_amount, fee_eur, fee_paid_at, fee_paid
           FROM orders WHERE paid_at IS NOT NULL OR fee_paid_at IS NOT NULL
           ORDER BY GREATEST(COALESCE(paid_at, 'epoch'), COALESCE(fee_paid_at, 'epoch')) DESC LIMIT 20""")
    stats = compute(await db.observations(30))
    first_obs = await db.first_observation()
    health = await db.missions_health()
    current = {r["code"]: r for r in await db.status()}

    def country(r):
        d = r["destination"]
        return DESTINATIONS[d][0] if d in DESTINATIONS else (r["mission_name"] or r["mission"])

    slots = []
    for h in health:
        s = stats.get(h["code"])
        cur = current.get(h["code"])
        slots.append({
            "code": h["code"], "name": h["name"],
            "open_now": bool(cur and cur["earliest"]), "earliest": cur["earliest"].isoformat() if cur and cur["earliest"] else None,
            "openings": len(s.episodes) if s else 0,
            "availability": round(100 * s.available / s.checks, 1) if s and s.checks else 0,
            "checks": s.checks if s else 0,
            "last_ok": h["fetched_at"].isoformat() if h["fetched_at"] else None,
            "failing": poller.health.fails.get(h["code"], 0) if poller else 0,
            "error": h["last_error"],
        })
    pipeline = [(k, STATUS[k], o[k] if k in o.keys() else 0) for k in ("awaiting_fee", "new", "in_progress", "booked")]
    pipeline.append(("paid", STATUS["paid"], o["paid"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tz": settings.tz,
        "price": settings.default_price,
        "users": {k: u[k] for k in ("total", "new_1d", "new_7d", "new_30d", "active_1d", "active_7d", "banned",
                                    "blocked_bot", "with_orders")},
        "daily_users": [{"day": r["day"].isoformat(), "n": r["n"]} for r in days],
        "sources": [{"source": r["source"], "n": r["n"], "n_7d": r["n_7d"]} for r in sources],
        "money": {"revenue_30d": _num(o["revenue_30d"]), "revenue_total": _num(o["revenue_total"]),
                  "fees_total": _num(o["fees_total"])},
        "pipeline": [{"key": k, "label": label, "n": n} for k, label, n in pipeline],
        "orders": [{
            "id": r["id"], "country": country(r), "status": r["status"], "status_label": STATUS.get(r["status"], r["status"]),
            "date_from": r["date_from"].isoformat() if r["date_from"] else None,
            "date_to": r["date_to"].isoformat() if r["date_to"] else None,
            "price": _num(r["price_eur"]) if r["price_eur"] is not None else None,
            "fee": _num(r["fee_eur"]), "appointment": r["appointment"], "created": r["created_at"].isoformat(),
        } for r in recent],
        "payments": [{
            "id": r["id"], "country": DESTINATIONS.get(r["destination"], (r["mission"],))[0],
            "service": _num(r["price_eur"]) if r["paid_at"] else None, "service_at": r["paid_at"].isoformat() if r["paid_at"] else None,
            "service_paid": f"{r['paid_amount'] or ''} {r['paid_asset'] or ''}".strip(),
            "fee": _num(r["fee_eur"]) if r["fee_paid_at"] else None, "fee_at": r["fee_paid_at"].isoformat() if r["fee_paid_at"] else None,
            "fee_paid": r["fee_paid"],
        } for r in payments],
        "support": {"questions_7d": await db.support_count(7), "docs_requests_30d": await db.event_total("docs_request", 30)},
        "slots": slots,
        "slots_since": first_obs.isoformat() if first_obs else None,
    }


def make_app(db: Database, settings: Settings, poller) -> web.Application:
    app = web.Application()

    async def page(_: web.Request) -> web.Response:
        return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})

    async def api(request: web.Request) -> web.Response:
        if not _authorized(request, settings):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return web.json_response(await collect(db, settings, poller), headers={"Cache-Control": "no-store"})
        except Exception as e:
            log.exception("panel data")
            return web.json_response({"error": str(e)[:200]}, status=500)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def robots(_: web.Request) -> web.Response:
        return web.Response(text="User-agent: *\nDisallow: /\n")

    app.router.add_get("/", health)
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/panel", page)
    app.router.add_get("/api/panel", api)
    return app


async def start(db: Database, settings: Settings, poller) -> web.AppRunner | None:
    port = os.getenv("PORT")
    if not port:
        log.info("PORT not set, dashboard disabled")
        return None
    runner = web.AppRunner(make_app(db, settings, poller), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("dashboard listening on :%s, public url %s", port, public_url())
    return runner

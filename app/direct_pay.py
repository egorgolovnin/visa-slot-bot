"""Direct USDT (TRC20) payments to our own address, matched automatically by a unique amount.

The client sends exactly e.g. 54.37 USDT from any wallet or exchange; we watch incoming USDT transfers to the
address through the public TronGrid API and match them by amount (to the cent) and time.
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone

import aiohttp

from . import payments

log = logging.getLogger("direct_pay")
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # Tether USD contract on Tron
VALID_HOURS = 48


def address() -> str | None:
    return os.getenv("PAY_TRC20_ADDRESS") or None


def enabled() -> bool:
    return bool(address())


async def eur_to_usdt(eur: float) -> float:
    """EUR -> USDT via Crypto Pay rates when available, otherwise a conservative fallback."""
    rate = None
    if payments.enabled():
        try:
            for r in await payments._call("getExchangeRates"):
                if r.get("source") == "USDT" and r.get("target") == "EUR" and r.get("is_valid", True):
                    rate = float(r["rate"])  # 1 USDT = rate EUR
                    break
        except Exception as e:
            log.warning("rates failed: %s", e)
    if not rate:
        rate = 1 / float(os.getenv("EUR_USDT_RATE", "1.17"))
    return eur / rate


async def unique_amount(db, eur: float) -> float:
    """Exact USDT equivalent (rounded up to the cent) plus a cent tag that is unique among all
    currently-pending payments, so one incoming transfer can never match two clients' orders."""
    import math
    base = math.ceil(await eur_to_usdt(eur) * 100) / 100
    taken = {round(float(a), 2) for a in await db.pending_direct_amounts()}
    cents = list(range(1, 100))
    random.shuffle(cents)
    for c in cents:
        amount = round(base + c / 100, 2)
        if amount not in taken:
            return amount
    # >99 pending orders sharing this base (practically never): bump whole USDT until unique.
    extra = 1
    while True:
        amount = round(base + extra + random.randint(1, 99) / 100, 2)
        if amount not in taken:
            return amount
        extra += 1


async def incoming_transfers(since: datetime) -> list[dict]:
    """USDT transfers to our address since `since`: [{tx, amount, ts}]."""
    url = f"https://api.trongrid.io/v1/accounts/{address()}/transactions/trc20"
    params = {"only_to": "true", "limit": "200", "contract_address": USDT_TRC20,
              "min_timestamp": str(int(since.timestamp() * 1000))}
    headers = {"accept": "application/json"}
    if os.getenv("TRONGRID_API_KEY"):
        headers["TRON-PRO-API-KEY"] = os.environ["TRONGRID_API_KEY"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(url, params=params, headers=headers) as r:
            data = await r.json(content_type=None)
    return parse_transfers(data, address())


def parse_transfers(data: dict, to_address: str | None) -> list[dict]:
    out = []
    for t in (data or {}).get("data") or []:
        info = t.get("token_info") or {}
        if (info.get("address") and info["address"] != USDT_TRC20) or (to_address and t.get("to") != to_address):
            continue
        decimals = int(info.get("decimals") or 6)
        out.append({"tx": t.get("transaction_id"), "amount": round(int(t.get("value", 0)) / 10 ** decimals, 2),
                    "ts": datetime.fromtimestamp(int(t.get("block_timestamp", 0)) / 1000, tz=timezone.utc)})
    return out


def match(pending: list, transfers: list[dict], used_tx: set[str]) -> list[tuple[object, dict]]:
    """Pair pending payments with transfers of exactly that amount sent after the payment was created."""
    pairs = []
    free = [t for t in transfers if t["tx"] not in used_tx]
    for p in pending:
        for t in free:
            if abs(t["amount"] - float(p["amount"])) < 0.005 and t["ts"] >= p["created_at"] - timedelta(minutes=2):
                pairs.append((p, t))
                free.remove(t)
                break
    return pairs

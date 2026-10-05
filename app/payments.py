"""Crypto Pay (@CryptoBot) invoices: price in EUR, paid in crypto. Enabled when CRYPTO_PAY_TOKEN is set."""
from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger("payments")

ASSETS = "USDT,TON,BTC,ETH,LTC,USDC,TRX,BNB"


class PaymentError(Exception):
    pass


def enabled() -> bool:
    return bool(os.getenv("CRYPTO_PAY_TOKEN"))


def _base() -> str:
    net = "testnet-pay.crypt.bot" if os.getenv("CRYPTO_PAY_TESTNET", "").lower() in ("1", "true") else "pay.crypt.bot"
    return f"https://{net}/api"


async def _call(method: str, **params) -> dict:
    headers = {"Crypto-Pay-API-Token": os.environ["CRYPTO_PAY_TOKEN"]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(f"{_base()}/{method}", json=params, headers=headers) as r:
            data = await r.json(content_type=None)
    if not data.get("ok"):
        raise PaymentError(f"{method}: {data.get('error')}")
    return data["result"]


async def create_invoice(order_id: int, amount_eur: float, description: str,
                         expires_in: int = 7 * 24 * 3600) -> tuple[int, str]:
    result = await _call(
        "createInvoice", currency_type="fiat", fiat="EUR", amount=f"{amount_eur:.2f}",
        accepted_assets=ASSETS, description=description[:1024], payload=f"order:{order_id}",
        expires_in=expires_in, allow_comments=False, allow_anonymous=False,
    )
    url = result.get("bot_invoice_url") or result.get("mini_app_invoice_url") or result.get("pay_url")
    return int(result["invoice_id"]), url


async def paid_invoices(invoice_ids: list[int]) -> dict[int, dict]:
    if not invoice_ids:
        return {}
    result = await _call("getInvoices", invoice_ids=",".join(map(str, invoice_ids)), status="paid")
    items = result.get("items", result) if isinstance(result, dict) else result
    return {int(i["invoice_id"]): i for i in items if i.get("status") == "paid"}


async def check() -> str:
    """Startup self-test: returns the app name Crypto Pay knows this token by."""
    me = await _call("getMe")
    return f"{me.get('name')} (app_id {me.get('app_id')})"


async def delete_invoice(invoice_id: int) -> None:
    """Make an unpaid invoice unpayable (used when re-issuing or cancelling). Errors are ignored."""
    try:
        await _call("deleteInvoice", invoice_id=int(invoice_id))
    except Exception as e:
        log.warning("deleteInvoice %s: %s", invoice_id, e)


async def invoice_statuses(invoice_ids: list[int]) -> dict[int, dict]:
    if not invoice_ids:
        return {}
    result = await _call("getInvoices", invoice_ids=",".join(map(str, invoice_ids)))
    items = result.get("items", result) if isinstance(result, dict) else result
    return {int(i["invoice_id"]): i for i in items}

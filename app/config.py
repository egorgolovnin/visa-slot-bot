import os
from dataclasses import dataclass


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


def _list(raw: str) -> tuple[str, ...]:
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    admin_ids: frozenset[int]
    proxy_url: str | None
    poll_interval: int          # seconds between full cycles
    missions: tuple[str, ...]   # empty = auto-discover which countries VFS serves in Cyprus
    country: str
    tz: str
    open_access: bool           # True: anyone who presses /start can use the bot; False: admin approves
    default_price: float        # EUR for 1 person (service)
    family_price: float         # flat total for a family: 2 adults + at least 1 child
    pdf_after_payment: bool     # True: the booking PDF is sent only once the invoice is paid
    max_order_countries: int    # how many countries one person can have open orders for at once
    docs_price: float = 250.0   # EUR for "we prepare the whole document package"


def load() -> Settings:
    missing = [k for k in ("BOT_TOKEN", "DATABASE_URL", "ADMIN_IDS") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")
    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        database_url=os.environ["DATABASE_URL"],
        admin_ids=_ids(os.environ["ADMIN_IDS"]),
        proxy_url=os.getenv("PROXY_URL") or None,
        poll_interval=int(os.getenv("POLL_INTERVAL_SEC", "600")),
        missions=_list(os.getenv("MISSIONS", "")),
        country=os.getenv("VFS_COUNTRY", "cyp"),
        tz=os.getenv("TZ_NAME", "Europe/Nicosia"),
        open_access=os.getenv("OPEN_ACCESS", "true").lower() in ("1", "true", "yes"),
        default_price=float(os.getenv("PRICE_EUR", "99")),
        family_price=float(os.getenv("FAMILY_PRICE", "149")),
        pdf_after_payment=os.getenv("PDF_AFTER_PAYMENT", "true").lower() in ("1", "true", "yes"),
        max_order_countries=int(os.getenv("MAX_ORDER_COUNTRIES", "2")),
        docs_price=float(os.getenv("DOCS_PRICE_EUR", "250")),
    )

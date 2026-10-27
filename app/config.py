import os
from dataclasses import dataclass


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


def _list(raw: str) -> tuple[str, ...]:
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _one_proxy(tok: str, gateway: str) -> str | None:
    """Accept a full URL, `login:pass@host:port`, or bare `login:pass` / `login pass`
    (bare creds get the shared gateway host:port prepended)."""
    tok = tok.strip()
    if not tok or tok.startswith("#"):
        return None
    if "://" in tok:
        return tok
    if "@" in tok:
        return "http://" + tok
    parts = tok.replace(":", " ").split()
    if len(parts) >= 4 and parts[1].isdigit():  # host:port:user:pass (формат монитора)
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) >= 2:
        return f"http://{parts[0]}:{parts[1]}@{gateway}"
    return None


def _proxies(raw: str, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Build the proxy pool from PROXY_URL (comma/newline-separated) plus lines from PROXY_FILE.
    Deduplicated, order preserved. Bare `login pass` entries use PROXY_GATEWAY."""
    gateway = os.getenv("PROXY_GATEWAY", "res.proxy-seller.com:10000")
    out: list[str] = []
    for tok in raw.replace("\n", ",").split(",") + list(extra):
        p = _one_proxy(tok, gateway)
        if p:
            out.append(p)
    return tuple(dict.fromkeys(out))


def _proxy_file_lines() -> tuple[str, ...]:
    path = os.getenv("PROXY_FILE", "config/proxies.txt")
    try:
        with open(path) as f:
            return tuple(f.read().replace(",", "\n").splitlines())
    except OSError:
        return ()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    admin_ids: frozenset[int]
    admin_chat_id: int | None  # Telegram group chat for admin alerts/analytics/backups; falls back to admin_ids
    channel: str | None        # public channel for announcements, e.g. @bestvisaeurope (bot must be its admin)
    manager: str | None        # human manager contact shown to clients, e.g. @bestvisamanager
    manager_name: str          # manager display name, e.g. Алена
    proxy_url: str | None        # first proxy (single-proxy consumers: cf, capsolver, vfs_auth, probes)
    proxies: tuple[str, ...]      # full pool for the monitoring clients (failover across them)
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
    canary_route: str | None = None  # a VFS route that reliably HAS slots (e.g. "are-fra") — parser self-test
    rk_fast: bool = True        # Germany (RK-Termin) has no login/VFS limit: poll it in its own tight loop
    rk_fast_interval: int = 15  # seconds to idle between RK-Termin checks (German slots can die in <2 min)

    @property
    def admin_targets(self) -> tuple[int, ...]:
        """Where admin alerts / analytics / backups go: the group chat if set, else the admin DMs."""
        if self.admin_chat_id:
            return (self.admin_chat_id,)
        return tuple(sorted(self.admin_ids))

    @property
    def manager_contact(self) -> str:
        """Client-facing manager contact line, or '' if not configured."""
        if not self.manager:
            return ""
        return f"👩‍💼 Ваш менеджер {self.manager_name} — {self.manager}"


def load() -> Settings:
    missing = [k for k in ("BOT_TOKEN", "DATABASE_URL", "ADMIN_IDS") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")
    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        database_url=os.environ["DATABASE_URL"],
        admin_ids=_ids(os.environ["ADMIN_IDS"]),
        admin_chat_id=(int(os.environ["ADMIN_CHAT_ID"]) if os.getenv("ADMIN_CHAT_ID") else None),
        channel=(os.getenv("CHANNEL", "").strip() or None),
        manager=(os.getenv("MANAGER", "").strip() or None),
        manager_name=(os.getenv("MANAGER_NAME", "Алена").strip()),
        proxy_url=(_proxies(os.getenv("PROXY_URL", ""), _proxy_file_lines()) or (None,))[0],
        proxies=_proxies(os.getenv("PROXY_URL", ""), _proxy_file_lines()),
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
        canary_route=(os.getenv("CANARY_ROUTE", "").strip().lower() or None),
        rk_fast=os.getenv("RK_FAST", "true").lower() in ("1", "true", "yes"),
        rk_fast_interval=int(os.getenv("RK_FAST_INTERVAL_SEC", "30")),
    )

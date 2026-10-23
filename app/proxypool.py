"""A tiny proxy pool with round-robin failover.

Lets a monitoring client spread its own requests across the user's own proxies and, when one
flakes/bans (503 tunnel, 429, connection reset), rotate to the next so it doesn't go blind on a
single-proxy outage. One proxy → rotate is a no-op (behaves exactly as before)."""
from __future__ import annotations


class ProxyPool:
    def __init__(self, proxies):
        if proxies is None:
            proxies = []
        elif isinstance(proxies, str):
            proxies = [proxies]
        self._p = [p for p in proxies if p]
        self._i = 0

    @property
    def size(self) -> int:
        return len(self._p)

    def current(self) -> str | None:
        return self._p[self._i] if self._p else None

    def rotate(self) -> str | None:
        if len(self._p) > 1:
            self._i = (self._i + 1) % len(self._p)
        return self.current()

    def __bool__(self) -> bool:
        return bool(self._p)

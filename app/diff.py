from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .vfs import Slot

OPENED, EARLIER, LATER, CLOSED = "opened", "earlier", "later", "closed"
NOTIFY_KINDS = (OPENED, EARLIER)


@dataclass(frozen=True)
class Change:
    kind: str
    slot: Slot
    old: date | None


def diff(prev: dict[tuple[str, str], date | None], current: list[Slot]) -> list[Change]:
    """Compare the stored state with a fresh fetch.

    VFS omits categories that have no date, so a pair that had a date and is now missing
    counts as closed.
    """
    changes: list[Change] = []
    seen = {s.key for s in current}
    for key, old in prev.items():
        if key not in seen and old is not None:
            centre, category = key
            changes.append(Change(CLOSED, Slot(current[0].mission if current else "", centre, category, None, ""), old))
    for s in current:
        if s.key not in prev:
            if s.earliest:
                changes.append(Change(OPENED, s, None))
            continue
        old = prev[s.key]
        new = s.earliest
        if old == new:
            continue
        if old is None:
            changes.append(Change(OPENED, s, None))
        elif new is None:
            changes.append(Change(CLOSED, s, old))
        elif new < old:
            changes.append(Change(EARLIER, s, old))
        else:
            changes.append(Change(LATER, s, old))
    return changes

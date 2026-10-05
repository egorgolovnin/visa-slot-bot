"""Slot statistics from the per-check observations log.

The public VFS endpoint only says "earliest available date", not how many slots exist, so what
we can measure is how often slots appear, how long they stay, how far ahead and at what hours.
"""
from __future__ import annotations

import html
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

GAP = timedelta(hours=1)  # bot was down longer than this: don't stretch an episode across the gap


@dataclass
class Episode:
    start: datetime
    end: datetime
    earliest: object
    open_now: bool = False

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass
class MissionStats:
    code: str
    name: str
    checks: int = 0
    available: int = 0
    first: datetime | None = None
    last: datetime | None = None
    episodes: list[Episode] = field(default_factory=list)
    n_dates: list[int] = field(default_factory=list)

    @property
    def covered_days(self) -> float:
        return max((self.last - self.first).total_seconds() / 86400, 0) if self.first and self.last else 0


def compute(rows) -> dict[str, MissionStats]:
    out: dict[str, MissionStats] = {}
    for r in rows:
        st = out.setdefault(r["mission"], MissionStats(r["mission"], r["name"] or r["mission"]))
        st.checks += 1
        st.first = st.first or r["at"]
        prev_at = st.last
        st.last = r["at"]
        current = st.episodes[-1] if st.episodes and st.episodes[-1].open_now else None
        if current and prev_at and r["at"] - prev_at > GAP:  # gap: close at the last seen moment
            current.open_now = False
            current = None
        if r["available"]:
            st.available += 1
            if r["n_dates"]:
                st.n_dates.append(r["n_dates"])
            if current:
                current.end = r["at"]
            else:
                st.episodes.append(Episode(r["at"], r["at"], r["earliest"], open_now=True))
        elif current:
            current.end = r["at"]
            current.open_now = False
    return out


def fmt_hours(h: float) -> str:
    if h < 1:
        return f"{max(1, round(h * 60))} мин"
    if h < 48:
        return f"{h:.0f} ч" if h >= 10 else f"{h:.1f} ч".replace(".0 ", " ")
    return f"{h / 24:.0f} дн"


def report(rows, days: int, tz: str, since: datetime | None) -> str:
    z = ZoneInfo(tz)
    stats = compute(rows)
    lines = [f"<b>📊 Слоты за {days} дн</b>"]
    if since:
        coverage = (datetime.now(since.tzinfo) - since).total_seconds() / 86400
        if coverage < days:
            lines.append(f"<i>Данные собираются с {since.astimezone(z).strftime('%d.%m %H:%M')} "
                         f"({coverage:.1f} дн) — цифры станут надёжными через 1–2 недели.</i>")
    lines.append("<i>VFS не отдаёт число слотов, поэтому считаем, сколько раз они открывались и сколько держались.</i>")
    ranked = sorted(stats.values(), key=lambda s: (-len(s.episodes), s.name))
    for s in ranked:
        lines.append("")
        lines.append(f"<b>{html.escape(s.name)}</b>")
        if not s.episodes:
            lines.append(f"  ⚪️ ни разу не открывались ({s.checks} проверок)")
            continue
        durations = [e.hours for e in s.episodes if not e.open_now] or [e.hours for e in s.episodes]
        per_month = len(s.episodes) / max(s.covered_days, 1 / 24) * 30
        share = 100 * s.available / s.checks if s.checks else 0
        leads = [(e.earliest - e.start.astimezone(z).date()).days for e in s.episodes if e.earliest]
        hours = Counter(e.start.astimezone(z).hour for e in s.episodes).most_common(3)
        lines.append(f"  🟢 открывались <b>{len(s.episodes)}</b> раз"
                     + (f" (≈{per_month:.0f} в месяц)" if s.covered_days >= 1 else ""))
        share_txt = "<1%" if 0 < share < 1 else f"{share:.0f}%"
        lines.append(f"  ⏱ держатся: обычно {fmt_hours(statistics.median(durations))}, "
                     f"максимум {fmt_hours(max(durations))} · слоты были {share_txt} времени")
        if leads:
            span = f"{min(leads)}" if min(leads) == max(leads) else f"{min(leads)}–{max(leads)}"
            lines.append(f"  📅 дата подачи через {span} дн после появления")
        if hours:
            lines.append("  🕐 чаще появляются в " + ", ".join(f"{h:02d}:00" for h, _ in hours))
        if s.n_dates and s.code == "deu":
            lines.append(f"  🗓 свободных дней в календаре: в среднем {statistics.mean(s.n_dates):.0f}, "
                         f"максимум {max(s.n_dates)}")
        if s.episodes[-1].open_now:
            lines.append(f"  👉 сейчас открыто, ближайшая дата {s.episodes[-1].earliest:%d.%m.%Y}")
    if not stats:
        lines.append("\nПока нет данных.")
    return "\n".join(lines)

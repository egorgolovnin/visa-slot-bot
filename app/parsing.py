"""Forgiving parsers for what people actually type."""
from __future__ import annotations

import re
from datetime import date

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
    "окт": 10, "ноя": 11, "дек": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
ANY_WORDS = {"любые", "любая", "любое", "любой", "все", "всё", "неважно", "не важно", "без разницы", "any", "-", "любые даты"}
NUM_DATE = re.compile(r"(\d{1,2})[./\-\s](\d{1,2})[./\-\s](\d{2,4})")
SHORT_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?![./\d])")
WORD_DATE = re.compile(r"(\d{1,2})\s*([a-zа-яё]{3,})\.?\s*(\d{4})?", re.I)


def _year(y: str | None, default: int) -> int:
    if not y:
        return default
    y = int(y)
    if y < 100:  # two-digit year: 26 -> 2026, 90 -> 1990
        y = y + 2000 if y + 2000 <= date.today().year + 10 else y + 1900
    return y


def parse_date(text: str, default_year: int | None = None) -> date | None:
    text = text.strip().lower()
    default_year = default_year or date.today().year
    m = NUM_DATE.fullmatch(text) or NUM_DATE.search(text)
    try:
        if m:
            return date(_year(m[3], default_year), int(m[2]), int(m[1]))
        m = WORD_DATE.search(text)
        if m and m[2][:3] in MONTHS:
            return date(_year(m[3], default_year), MONTHS[m[2][:3]], int(m[1]))
        m = SHORT_DATE.fullmatch(text)
        if m:
            return date(default_year, int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None


def parse_range(text: str) -> tuple[str, date | None, date | None]:
    """('any'|'ok'|'error', from, to). Accepts '01.10.2026 - 10.10.2026', '1.10–10.10.26',
    'с 1 ноября по 20 ноября 2026', a single date (= from that day on), or 'любые'."""
    t = text.strip().lower()
    if t in ANY_WORDS:
        return "any", None, None
    my = re.fullmatch(r"(\d{1,2})[./](\d{4})", t)  # "11.2026" = the whole month
    if my and 1 <= int(my.group(1)) <= 12:
        d1, d2 = _month_bounds(int(my.group(1)), int(my.group(2)), date.today())
        return "ok", max(d1, date.today()), d2
    if not re.search(r"\d{1,2}[./]\d{1,2}", t):  # no numeric dates: try words ("ноябрь", "конец ноября"...)
        human = parse_human_window(t)
        if human:
            return "ok", human[0], human[1]
    t = re.sub(r"[–—−]", "-", t)
    t = re.sub(r"^\s*с\s+", "", t)
    parts = re.split(r"\s+-\s+|\s*-\s*(?=\d{1,2}[./\s])|\s+(?:по|до|to|till|until)\s+", t, maxsplit=1)
    if len(parts) == 2:
        right = parse_date(parts[1])
        left = parse_date(parts[0], default_year=right.year if right else None)
        if left and right:
            return "ok", left, right
        return "error", None, None
    single = parse_date(t)
    return ("ok", single, None) if single else ("error", None, None)


def translit(text: str) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y",
        "ь": "", "э": "e", "ю": "iu", "я": "ia",
    })
    return text.lower().translate(table).upper()


# ---- nationality ----
RU_COUNTRIES = {
    "россия": "Russia", "рф": "Russia", "российская федерация": "Russia", "украина": "Ukraine",
    "беларусь": "Belarus", "белоруссия": "Belarus", "казахстан": "Kazakhstan", "узбекистан": "Uzbekistan",
    "армения": "Armenia", "грузия": "Georgia", "азербайджан": "Azerbaijan", "кыргызстан": "Kyrgyzstan",
    "киргизия": "Kyrgyzstan", "таджикистан": "Tajikistan", "туркменистан": "Turkmenistan", "молдова": "Moldova",
    "индия": "India", "китай": "China", "иран": "Iran", "израиль": "Israel", "турция": "Turkey",
    "ливан": "Lebanon", "египет": "Egypt", "сирия": "Syria", "пакистан": "Pakistan", "филиппины": "Philippines",
    "вьетнам": "Vietnam", "шри-ланка": "Sri Lanka", "непал": "Nepal", "бангладеш": "Bangladesh", "иордания": "Jordan",
    "монголия": "Mongolia", "сербия": "Serbia", "нигерия": "Nigeria", "юар": "South Africa",
}
EN_ALIASES = {"rf": "Russia", "russian federation": "Russia", "uk": "United Kingdom", "usa": "United States",
              "us": "United States", "kyrgyz republic": "Kyrgyzstan", "turkiye": "Turkey", "türkiye": "Turkey"}


def _country_names() -> dict[str, str]:
    import pycountry
    names: dict[str, str] = {}
    for c in pycountry.countries:
        short = getattr(c, "common_name", None) or c.name
        for n in {c.name, short, getattr(c, "official_name", c.name)}:
            names[n.lower()] = short
    # friendlier spellings
    for k, v in {"russian federation": "Russia", "iran, islamic republic of": "Iran", "viet nam": "Vietnam",
                 "korea, republic of": "South Korea", "moldova, republic of": "Moldova",
                 "syrian arab republic": "Syria", "türkiye": "Turkey"}.items():
        names[k] = v
    for k, v in list(names.items()):
        if v in ("Russian Federation",):
            names[k] = "Russia"
    for v in set(names.values()):          # the canonical short names themselves: "russia", "vietnam"...
        names[v.lower()] = v
    return names


_NAMES: dict[str, str] | None = None


def normalize_country(text: str) -> tuple[str | None, list[str]]:
    """(country, suggestions). Exact/alias match -> country; otherwise up to 3 close suggestions."""
    import difflib
    global _NAMES
    if _NAMES is None:
        _NAMES = _country_names()
    t = re.sub(r"\s+", " ", text.strip().lower())
    for table in (RU_COUNTRIES, EN_ALIASES, _NAMES):
        if t in table:
            return table[t], []
    pool = {**_NAMES, **RU_COUNTRIES, **EN_ALIASES}
    close = difflib.get_close_matches(t, pool.keys(), n=5, cutoff=0.5)
    seen, out = set(), []
    for k in close:
        v = pool[k]
        if v not in seen:
            seen.add(v)
            out.append(v)
    return None, out[:3]


# ---- human date windows: "ноябрь", "конец ноября", "5-20 ноября", "ближайшие 2 недели"... ----
import calendar
from datetime import timedelta

MONTH_STEMS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9,
    "октябр": 10, "ноябр": 11, "декабр": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_MONTH_WORD = re.compile(r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|"
                         r"октябр\w*|ноябр\w*|декабр\w*|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|"
                         r"sep\w*|oct\w*|nov\w*|dec\w*)\b(?:\s+(\d{4}|\d{2})\b)?", re.I)
_NUM_WORDS = {"один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
              "пару": 2, "пара": 2, "несколько": 3}


def _month_no(word: str) -> int | None:
    w = word.lower()
    if w.startswith("ма") and not w.startswith("мар"):
        return 5
    for stem, n in MONTH_STEMS.items():
        if stem != "ма" and w.startswith(stem):
            return n
    return None


def _month_bounds(month: int, year: int | None, today: date) -> tuple[date, date]:
    if year is None:
        year = today.year if month >= today.month else today.year + 1
    elif year < 100:
        year += 2000
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def parse_human_window(text: str, today: date | None = None) -> tuple[date, date | None] | None:
    """Understand windows written in words. Returns (from, to|None) or None."""
    today = today or date.today()
    t = re.sub(r"\s+", " ", text.strip().lower().replace("ё", "е"))
    t = re.sub(r"[–—−]", "-", t)

    if re.search(r"как можно скорее|срочно|asap|побыстрее|поскорее|быстрее|ближайш\w* врем", t):
        return today, None
    if re.fullmatch(r"(на )?сегодня", t):
        return today, today
    if re.fullmatch(r"(на )?завтра", t):
        return today + timedelta(days=1), today + timedelta(days=1)

    # relative periods
    if re.search(r"следующ\w* недел", t):
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=6)
    if re.search(r"следующ\w* месяц", t):
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return _month_bounds(nxt.month, nxt.year, today)
    m = re.search(r"(ближайш\w*|в течени\w*)\s+(\d+|\w+)?\s*(дн\w*|день|недел\w*|месяц\w*)", t)
    if m:
        n_raw = m.group(2) or "1"
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        unit = m.group(3)
        days = n if unit.startswith(("дн", "ден")) else n * 7 if unit.startswith("недел") else n * 30
        return today, today + timedelta(days=days)
    if re.search(r"(эт\w*|текущ\w*) недел", t):
        return today, today + timedelta(days=6 - today.weekday())
    if re.search(r"следующ\w* недел", t):
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=6)
    if re.search(r"(эт\w*|текущ\w*) месяц", t):
        return today, _month_bounds(today.month, today.year, today)[1]
    if re.search(r"следующ\w* месяц", t):
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return _month_bounds(nxt.month, nxt.year, today)
    m = re.search(r"через (\d+|\w+)?\s*(дн\w*|день|недел\w*|месяц\w*)", t)
    if m:
        n_raw = m.group(1) or "1"
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        unit = m.group(2)
        days = n if unit.startswith(("дн", "ден")) else n * 7 if unit.startswith("недел") else n * 30
        return today + timedelta(days=days), None

    months = [(_month_no(mm.group(1)), mm.group(2)) for mm in _MONTH_WORD.finditer(t)]
    months = [(mn, int(y) if y else None) for mn, y in months if mn]

    # "5-20 ноября", "с 5 по 20 ноября", "5 - 20 nov"
    m = re.search(r"(\d{1,2})\s*(?:-|по|до)\s*(\d{1,2})\s+" + _MONTH_WORD.pattern[2:], t, re.I)
    if m and months:
        mn, yr = months[0]
        start, end = _month_bounds(mn, yr, today)
        try:
            return start.replace(day=int(m.group(1))), start.replace(day=int(m.group(2)))
        except ValueError:
            return None

    # "до 15 ноября", "до конца ноября", "до ноября"
    m = re.search(r"\bдо\b", t)
    if m and months and not re.search(r"\b(с|от|после)\b", t):
        mn, yr = months[-1]
        start, end = _month_bounds(mn, yr, today)
        day = re.search(r"до (\d{1,2})\s", t + " ")
        if day:
            try:
                return today, start.replace(day=int(day.group(1)))
            except ValueError:
                return None
        if re.search(r"до (конца|окончания)", t):
            return today, end
        return today, start - timedelta(days=1)

    # "после 10 ноября", "с середины ноября", "с 10 ноября" (open end)
    if re.search(r"\b(после|с|от|начиная)\b", t) and months and len(months) == 1 and not re.search(r"\bпо\b", t):
        mn, yr = months[0]
        start, end = _month_bounds(mn, yr, today)
        day = re.search(r"(\d{1,2})\s", t + " ")
        if re.search(r"середин", t):
            return max(today, start.replace(day=10)), None
        if re.search(r"конц|конец", t):
            return max(today, start.replace(day=20)), None
        if day:
            try:
                d0 = start.replace(day=int(day.group(1)))
                return max(today, d0 + timedelta(days=1) if "после" in t else d0), None
            except ValueError:
                return None
        return max(today, start), None

    if months:
        # part of a month
        mn, yr = months[0]
        start, end = _month_bounds(mn, yr, today)
        if len(months) >= 2:  # "ноябрь-декабрь", "ноябрь или декабрь"
            end = _month_bounds(months[-1][0], months[-1][1] or start.year + (months[-1][0] < mn), today)[1]
        elif re.search(r"начал|перв\w* половин", t):
            end = start.replace(day=15)
        elif re.search(r"середин", t):
            start, end = start.replace(day=10), start.replace(day=20)
        elif re.search(r"конц|конец|втор\w* половин", t):
            start = start.replace(day=16)
        return max(start, today), end

    # "11.2026", "11/2026"
    m = re.fullmatch(r"(\d{1,2})[./](\d{4})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        start, end = _month_bounds(int(m.group(1)), int(m.group(2)), today)
        return max(start, today), end
    return None

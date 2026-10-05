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

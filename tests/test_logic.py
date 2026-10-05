from datetime import date

from app.diff import CLOSED, EARLIER, LATER, OPENED, diff
from app.vfs import parse_date, parse_response

SAMPLE = {
    "lastUpdatedOn": "05 Oct 2026 10:12",
    "vacList": [{
        "vacName": "Nicosia",
        "visaGroupList": [
            {"displayName": "Schengen Visa- Less than 90 days", "earliestAvailableDate": "23 Oct 2026"},
            {"displayName": "Long Stay D visa", "earliestAvailableDate": ""},
        ],
    }],
}


def test_parse_date_formats():
    assert parse_date("23 Oct 2026") == date(2026, 10, 23)
    assert parse_date("2026-10-23T00:00:00") == date(2026, 10, 23)
    assert parse_date("") is None
    assert parse_date("garbage") is None


def test_parse_response():
    slots = parse_response("hun", SAMPLE)
    assert len(slots) == 2
    assert slots[0].centre == "Nicosia" and slots[0].earliest == date(2026, 10, 23)
    assert slots[1].earliest is None


def test_diff_kinds():
    slots = parse_response("hun", SAMPLE)
    k1, k2 = slots[0].key, slots[1].key
    assert diff({k1: None, k2: None}, slots)[0].kind == OPENED
    assert diff({k1: date(2026, 11, 1), k2: None}, slots)[0].kind == EARLIER
    assert diff({k1: date(2026, 10, 1), k2: None}, slots)[0].kind == LATER
    assert diff({k1: date(2026, 10, 23), k2: date(2026, 12, 1)}, slots)[0].kind == CLOSED
    assert diff({k1: date(2026, 10, 23), k2: None}, slots) == []


def test_new_pair_only_reported_if_has_date():
    slots = parse_response("hun", SAMPLE)
    changes = diff({}, slots)
    assert [c.kind for c in changes] == [OPENED]


def test_missing_pair_counts_as_closed():
    slots = parse_response("hun", SAMPLE)
    prev = {("Nicosia", "Gone category"): date(2026, 10, 30), slots[0].key: date(2026, 10, 23)}
    changes = diff(prev, slots)
    assert [(c.kind, c.slot.category) for c in changes] == [(CLOSED, "Gone category")]
    # nothing returned at all: everything with a date closes
    assert [c.kind for c in diff({slots[0].key: date(2026, 10, 23)}, [])] == [CLOSED]

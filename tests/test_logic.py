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


def test_rktermin_parsers():
    from app.rktermin import find_schengen_category, parse_days, parse_form
    cats = ('<div><a href="appointment_showMonth.do?locationCode=niko&amp;realmId=926&amp;categoryId=1111">Appointments for '
            'the application of Schengen visas (stay for less than 90 days) at the German Embassy, Ayii Omologites</a></div>'
            '<div><a href="appointment_showMonth.do?locationCode=niko&amp;realmId=926&amp;categoryId=2222">Spouses</a></div>')
    assert find_schengen_category(cats) == "1111"
    form = ('<form id="appointment_captcha_month" name="appointment_captcha_month" action="/rktermin/extern/appointment_showMonth.do" method="post">'
            '<input type="hidden" name="locationCode" value="niko" id="x"/><input type="text" name="captchaText" value="" id="appointment_captcha_month_captchaText"/>'
            '<input type="submit" name="action:appointment_showMonth" value="Continue"/></form>')
    action, fields = parse_form(form)
    assert action.endswith("appointment_showMonth.do") and fields["locationCode"] == "niko" and "captchaText" in fields
    page = ('<a href="appointment_showDay.do?locationCode=niko&amp;realmId=926&amp;categoryId=1111&amp;dateStr=24.11.2026">24</a>'
            '<a href="appointment_showDay.do?locationCode=niko&amp;realmId=926&amp;categoryId=1111&amp;dateStr=12.11.2026">12</a>')
    assert [d.isoformat() for d in parse_days(page)] == ["2026-11-12", "2026-11-24"]


def test_rktermin_only_continue_button_is_sent():
    import inspect
    from app import rktermin
    src = inspect.getsource(rktermin.RkTerminClient._pass_captcha)
    assert 'not k.startswith("action:")' in src


def test_supersaas_parsing():
    from datetime import date as d
    from app.supersaas import no_free_slots, parse_free_dates
    page = "<table><tr><td>Mon 12 Oct 2099 09:00</td></tr><tr><td>Oct 3, 2099</td></tr><tr><td>1 Jan 2000</td></tr></table>"
    assert parse_free_dates(page) == [d(2099, 10, 3), d(2099, 10, 12)]
    assert no_free_slots("<p>No available space found</p>")


def test_direct_pay_parse_and_match():
    from datetime import datetime, timedelta, timezone
    from app import direct_pay
    addr = "TX2kGRwhdDhWj69BYwzZtej7zsFyzq3wZU"
    now = datetime.now(timezone.utc)
    data = {"data": [
        {"transaction_id": "aa", "block_timestamp": int(now.timestamp() * 1000), "to": addr, "value": "57930000",
         "token_info": {"symbol": "USDT", "address": direct_pay.USDT_TRC20, "decimals": 6}},
        {"transaction_id": "bb", "block_timestamp": int(now.timestamp() * 1000), "to": "Tother", "value": "57930000",
         "token_info": {"symbol": "USDT", "address": direct_pay.USDT_TRC20, "decimals": 6}},
        {"transaction_id": "cc", "block_timestamp": int(now.timestamp() * 1000), "to": addr, "value": "57930000",
         "token_info": {"symbol": "FAKE", "address": "Tfake", "decimals": 6}},
    ]}
    t = direct_pay.parse_transfers(data, addr)
    assert [x["tx"] for x in t] == ["aa"] and t[0]["amount"] == 57.93
    pending = [{"id": 1, "amount": 57.93, "created_at": now - timedelta(minutes=1)},
               {"id": 2, "amount": 57.94, "created_at": now - timedelta(minutes=1)}]
    pairs = direct_pay.match(pending, t, set())
    assert [(p["id"], tr["tx"]) for p, tr in pairs] == [(1, "aa")]
    assert direct_pay.match(pending, t, {"aa"}) == []


def test_parse_start_payload():
    from app.deeplink import parse_start_payload
    assert parse_start_payload("wheel-site_home") == ("wheel", "site_home")
    assert parse_start_payload("wheel") == ("wheel", "wheel")
    assert parse_start_payload("invite-site_ref") == ("invite", "site_ref")
    assert parse_start_payload("order-site_blog_vfs") == ("order", "site_blog_vfs")
    assert parse_start_payload("community") == (None, "community")
    assert parse_start_payload("wheelx") == (None, "wheelx")
    assert parse_start_payload("") == (None, "")
    assert parse_start_payload(None) == (None, "")
    assert parse_start_payload("wheel-a b<c>") == ("wheel", "abc")

"""Mock PostHog event fixture for the forwarder portal (stdlib only)."""
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE_URL = "http://localhost:8000/app/#"
INVOICE_PATH = "/invoices/INV-2291"
NAMED_USERS = ["u-anna", "u-ben", "u-carla"]
GENERIC_USERS = [
    "u-david", "u-elif", "u-felix", "u-greta", "u-hannes", "u-ines", "u-jonas",
    "u-katrin", "u-lukas", "u-marie", "u-nora", "u-oskar", "u-paula", "u-quentin",
    "u-rosa", "u-stefan", "u-tobias",
]
USERS = NAMED_USERS + GENERIC_USERS

TAIL = 'div:attr__id="view";main;body'
NAV = {
    "shipments": 'a.nav-link:attr__data-route="shipments"href="#/shipments"text="Shipments";nav;header;body',
    "invoice": 'a.nav-link:attr__data-route="invoice"href="#/invoices/INV-2291"text="Invoices";nav;header;body',
    "booking": 'a.nav-link:attr__data-route="booking"href="#/booking"text="Book shipment";nav;header;body',
}
FEEDBACK_BTN = 'button.btn:attr__id="feedback-button"text="Feedback";header;body'
DOWNLOAD_PDF = f'button.btn.btn-primary:attr__id="download-pdf"text="Download PDF";div.actions;{TAIL}'
ETA_TEXTS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
HAWBS = ["HAWB-784512", "HAWB-784519", "HAWB-784533", "HAWB-784540", "HAWB-784561"]


def eta_span(eta):
    return f'span:nth-child="1"nth-of-type="1"text="{eta}";td.eta-cell;tr;tbody;table:attr__id="shipments-table";{TAIL}'


SCREENS = {
    "shipments": {
        "path": "/shipments",
        "clicks": [f'td:text="{h}";tr;tbody;table:attr__id="shipments-table";{TAIL}' for h in HAWBS]
        + [f'td.weight-cell:text="412.5";tr;tbody;table:attr__id="shipments-table";{TAIL}',
           f'th:attr__id="weight-header"text="Weight (lbs)";tr;thead;table:attr__id="shipments-table";{TAIL}',
           eta_span("2026-11-03"), NAV["invoice"], NAV["booking"], FEEDBACK_BTN],
    },
    "invoice": {
        "path": INVOICE_PATH,
        "clicks": [f'td:text="Air freight FRA-ORD";tr;tbody;table:attr__id="invoice-lines";{TAIL}',
                   f'td:text="Fuel surcharge";tr;tbody;table:attr__id="invoice-lines";{TAIL}',
                   f'div.invoice-total:text="EUR 4,812.40";{TAIL}',
                   NAV["shipments"], NAV["booking"], FEEDBACK_BTN],
    },
    "booking": {
        "path": "/booking",
        "clicks": [f'input:attr__name="{n}";label;form:attr__id="booking-form";{TAIL}'
                   for n in ("origin", "destination", "pieces", "weight", "ready_date")]
        + [f'button.btn.btn-primary:attr__type="submit"text="Create booking";form:attr__id="booking-form";{TAIL}',
           NAV["shipments"], NAV["invoice"]],
    },
}

NOISE_RAGE = [
    f'button.btn.btn-primary:attr__type="submit"text="Create booking";form:attr__id="booking-form";{TAIL}',
    f'input:attr__name="ready_date";label;form:attr__id="booking-form";{TAIL}',
    NAV["invoice"],
    f'th:text="Status";tr;thead;table:attr__id="shipments-table";{TAIL}',
]
NOISE_DEAD = [
    f'div.invoice-total:text="EUR 4,812.40";{TAIL}',
    'img.logo:attr__alt="Forwarder portal";header;body',
    f'h1:text="Shipments";{TAIL}',
    f'td:text="Fuel surcharge";tr;tbody;table:attr__id="invoice-lines";{TAIL}',
    f'span.badge:text="In transit";td;tr;tbody;table:attr__id="shipments-table";{TAIL}',
]
SCREEN_OF_CHAIN = {}
for _name, _s in SCREENS.items():
    for _c in _s["clicks"]:
        SCREEN_OF_CHAIN.setdefault(_c, _name)
NOISE_SCREEN = {NOISE_RAGE[0]: "booking", NOISE_RAGE[1]: "booking", NOISE_RAGE[2]: "shipments",
                NOISE_RAGE[3]: "shipments", NOISE_DEAD[0]: "invoice", NOISE_DEAD[1]: "shipments",
                NOISE_DEAD[2]: "shipments", NOISE_DEAD[3]: "invoice", NOISE_DEAD[4]: "shipments"}

P1_ERROR = ("TypeError", "Cannot read properties of null (reading 'href')")


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _uuid(rng):
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _event(rng, name, user, session, dt, screen, chain=None, exc=None):
    path = SCREENS[screen]["path"]
    props = {
        "$current_url": BASE_URL + path,
        "$pathname": path,
        "$session_id": session,
        "$host": "localhost:8000",
        "$browser": "Chrome",
        "$os": "Mac OS X",
    }
    if chain:
        props["$elements_chain"] = chain
    if name == "$autocapture":
        props["$event_type"] = "click"
    if exc:
        props["$exception_type"], props["$exception_message"] = exc
        props["$exception_source"] = "http://localhost:8000/app/app.js"
    return {"uuid": _uuid(rng), "event": name, "distinct_id": user, "timestamp": _ts(dt), "properties": props}


def _normal_session(rng, user, start, allow_download):
    session = _uuid(rng)
    events = []
    t = start
    screen = rng.choice(["shipments", "shipments", "invoice", "booking"])
    for _ in range(rng.randint(2, 4)):
        events.append(_event(rng, "$pageview", user, session, t, screen))
        clicks = SCREENS[screen]["clicks"] + ([DOWNLOAD_PDF] if screen == "invoice" and allow_download else [])
        for _ in range(rng.randint(1, 4)):
            t += timedelta(seconds=rng.uniform(3, 45))
            events.append(_event(rng, "$autocapture", user, session, t, screen, rng.choice(clicks)))
        t += timedelta(seconds=rng.uniform(5, 60))
        screen = rng.choice([s for s in SCREENS if s != screen])
    return events


def _traffic(rng, start, hours, sessions_per_user, allow_download):
    events = []
    for user in USERS:
        for _ in range(sessions_per_user):
            t = start + timedelta(seconds=rng.uniform(0, hours * 3600 - 1800))
            events += _normal_session(rng, user, t, allow_download(t))
    return events


def _noise(rng, start, days):
    events = []
    for d in range(days):
        day = start + timedelta(days=d)
        rage = rng.sample(NOISE_RAGE, 2)
        dead = rng.sample(NOISE_DEAD, 2)
        for name, chains in (("$rageclick", rage), ("$dead_click", dead)):
            for chain in chains:
                t = day + timedelta(seconds=rng.uniform(3600, 23 * 3600))
                events.append(_event(rng, name, rng.choice(USERS), _uuid(rng), t, NOISE_SCREEN[chain], chain))
        t = day + timedelta(seconds=rng.uniform(3600, 23 * 3600))
        events.append(_event(rng, "$exception", rng.choice(USERS), _uuid(rng), t, rng.choice(list(SCREENS)),
                             exc=("Error", "ResizeObserver loop completed with undelivered notifications.")))
    return events


def _p1(rng, day_start, users, per_user, success):
    """Invoice download clicks: broken (dead click + TypeError) or working (autocapture only)."""
    events = []
    for user, n in zip(users, per_user):
        session = _uuid(rng)
        t = day_start + timedelta(seconds=rng.uniform(7 * 3600, 19 * 3600))
        events.append(_event(rng, "$pageview", user, session, t, "invoice"))
        for _ in range(n):
            t += timedelta(seconds=rng.uniform(2, 12))
            events.append(_event(rng, "$autocapture", user, session, t, "invoice", DOWNLOAD_PDF))
            if not success:
                events.append(_event(rng, "$exception", user, session, t + timedelta(milliseconds=4),
                                     "invoice", exc=P1_ERROR))
                events.append(_event(rng, "$dead_click", user, session, t + timedelta(seconds=2.5),
                                     "invoice", DOWNLOAD_PDF))
    return events


def _p3(rng, day_start, users, per_user):
    events = []
    for user, n in zip(users, per_user):
        session = _uuid(rng)
        t = day_start + timedelta(seconds=rng.uniform(6 * 3600, 20 * 3600))
        events.append(_event(rng, "$pageview", user, session, t, "shipments"))
        for _ in range(n):
            t += timedelta(seconds=rng.uniform(4, 40))
            chain = eta_span(rng.choice(ETA_TEXTS))
            events.append(_event(rng, "$autocapture", user, session, t, "shipments", chain))
            events.append(_event(rng, "$rageclick", user, session, t + timedelta(seconds=0.6), "shipments", chain))
    return events


P1_PER_USER = [3, 3, 2, 2, 2, 2]
P3_PER_USER = [6, 5, 5, 5, 4, 4, 4, 4, 4]


def build_events(rng):
    start = datetime(2026, 10, 29, tzinfo=timezone.utc)
    last_day = datetime(2026, 10, 31, tzinfo=timezone.utc)
    events = _traffic(rng, start, 72, 12, lambda t: t < last_day)
    events += _noise(rng, start, 3)
    others = [u for u in USERS if u != "u-anna"]
    events += _p1(rng, last_day, ["u-anna"] + rng.sample(others, 5), P1_PER_USER, success=False)
    others = [u for u in USERS if u != "u-carla"]
    events += _p3(rng, last_day, ["u-carla"] + rng.sample(others, 8), P3_PER_USER)
    events.sort(key=lambda e: e["timestamp"])
    return events


def build_after_fix_events(rng):
    start = datetime(2026, 11, 1, 18, tzinfo=timezone.utc)
    events = _traffic(rng, start, 24, 4, lambda t: True)
    events += _p1(rng, start, rng.sample(USERS, 6), P1_PER_USER, success=True)
    events += _p3(rng, start, rng.sample(USERS, 9), [5, 5, 5, 4, 4, 4, 4, 4, 4])
    events.sort(key=lambda e: e["timestamp"])
    return events


def _is_eta(e):
    return "td.eta-cell" in e["properties"].get("$elements_chain", "")


def _is_pdf(e):
    return 'attr__id="download-pdf"' in e["properties"].get("$elements_chain", "")


if __name__ == "__main__":
    ev = build_events(random.Random(42))
    after = build_after_fix_events(random.Random(42))
    last24 = [e for e in ev if e["timestamp"] >= "2026-10-31T00:00:00"]

    p1_dead = [e for e in ev if e["event"] == "$dead_click" and _is_pdf(e)]
    p1_exc = [e for e in ev if e["event"] == "$exception" and e["properties"]["$exception_type"] == "TypeError"]
    p3_rage = [e for e in ev if e["event"] == "$rageclick" and _is_eta(e)]
    assert len(p1_dead) == 14 and all(e in last24 for e in p1_dead)
    assert len({e["distinct_id"] for e in p1_dead}) == 6 and "u-anna" in {e["distinct_id"] for e in p1_dead}
    assert all(e["properties"]["$pathname"] == INVOICE_PATH for e in p1_dead)
    assert len(p1_exc) == 14 and {e["properties"]["$session_id"] for e in p1_exc} == {e["properties"]["$session_id"] for e in p1_dead}
    assert len(p3_rage) == 41 and all(e in last24 for e in p3_rage)
    assert len({e["distinct_id"] for e in p3_rage}) == 9
    assert all(e["properties"]["$pathname"] == "/shipments" for e in p3_rage)

    noise = Counter(e["properties"]["$elements_chain"] for e in ev
                    if e["event"] in ("$rageclick", "$dead_click") and not _is_eta(e) and not _is_pdf(e))
    noise_days = Counter(e["timestamp"][:10] for e in ev
                         if e["event"] in ("$rageclick", "$dead_click") and not _is_eta(e) and not _is_pdf(e))
    assert max(noise.values()) <= 4 and len(noise_days) == 3

    a_dead = [e for e in after if e["event"] == "$dead_click" and _is_pdf(e)]
    a_exc = [e for e in after if e["event"] == "$exception" and e["properties"]["$exception_type"] == "TypeError"]
    a_ok = [e for e in after if e["event"] == "$autocapture" and _is_pdf(e)]
    a_rage = [e for e in after if e["event"] == "$rageclick" and _is_eta(e)]
    assert not a_dead and not a_exc and a_ok and 30 <= len(a_rage) <= 50
    assert all("2026-11-01T18" <= e["timestamp"] < "2026-11-02T18" for e in after)

    print(f"main: {len(ev)} events, {len({e['distinct_id'] for e in ev})} users, "
          f"{ev[0]['timestamp']} .. {ev[-1]['timestamp']}")
    print(f"P1: {len(p1_dead)} dead clicks on #download-pdf, {len(p1_exc)} TypeErrors, "
          f"{len({e['distinct_id'] for e in p1_dead})} users (last 24 h)")
    print(f"P3: {len(p3_rage)} rage clicks on td.eta-cell span, "
          f"{len({e['distinct_id'] for e in p3_rage})} users (last 24 h)")
    print(f"noise rage/dead: {sum(noise.values())} over {len(noise)} elements, max {max(noise.values())}/element, "
          f"per day {dict(sorted(noise_days.items()))}")
    print(f"after-fix: {len(after)} events, #download-pdf ok clicks {len(a_ok)}, dead {len(a_dead)}, "
          f"TypeError {len(a_exc)}, ETA rage {len(a_rage)} "
          f"({len({e['distinct_id'] for e in a_rage})} users)")
    print("OK")

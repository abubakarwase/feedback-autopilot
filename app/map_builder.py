"""Shared map: one ScreenCard per screen, built from PostHog usage, signals, labels and impact checks.

Usage numbers are computed in Python. The LLM only writes the prose (what happens, how people use it,
where it breaks) from those facts; words, what changed and sources are always the Python values, so
the card never claims a fix, a term or a source that is not in the data. With OFFLINE=1 the whole card
is built in Python.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.llm import llm
from app.schemas import ScreenCard, ScreenView, Source, Word
from app.tools import labels, posthog, slack

SCREENS = {"shipments": "/shipments", "invoice": "/invoices/:id", "booking": "/booking"}
APP_JS = Path("target-app/app.js")
PROBLEM_EVENTS = ("$rageclick", "$dead_click", "$exception")
MERGED = ("merged_unchanged", "edited")
MIN_GROUP_EVENTS = 5  # same threshold as the impact check: smaller groups are noise
REQUIRED_TABLES = ("signals", "replies", "labels", "impact_checks", "screen_cards")

# What each screen is, read from target-app/app.js. Words are (forwarder term, code name).
ABOUT = {
    "shipments": {
        "forwarder": "Lists your active house bills (HAWB) with origin, destination, weight, "
                     "chargeable weight, status and ETA.",
        "developer": "renderShipments(view) renders the SHIPMENTS array into table#shipments-table; "
                     "it is the default route (#/shipments).",
        "words": [("HAWB", "house_bill_no"), ("Origin", "origin"), ("Destination", "destination"),
                  ("Weight", "weight_kg"), ("Chargeable weight", "chargeable_kg"), ("Status", "status"),
                  ("ETA", "eta_at")],
    },
    "invoice": {
        "forwarder": "Shows one invoice: customer, HAWB, issue and due date, the line items and the total. "
                     "You can download it as a PDF.",
        "developer": "renderInvoice(view, id) renders INVOICES[id] for #/invoices/:id; the Download PDF "
                     "button (#download-pdf) calls downloadInvoicePdf(inv), which builds the file with "
                     "buildInvoicePdf(inv).",
        "words": [("Invoice number", "id"), ("Customer", "customer"), ("HAWB", "house_bill_no"),
                  ("Issued", "issued"), ("Due", "due"), ("Line items", "lines"), ("Total", "invoice-total"),
                  ("Download PDF", "downloadInvoicePdf")],
    },
    "booking": {
        "forwarder": "Request an air freight pickup: origin, destination, pieces, weight in kg and ready date. "
                     "You get a booking number back right away.",
        "developer": "renderBooking(view) renders form#booking-form; on submit it builds a BKG-2026-* number "
                     "in the browser (bookingCounter, no backend call) and shows #booking-confirmation.",
        "words": [("Origin", "origin"), ("Destination", "destination"), ("Pieces", "pieces"),
                  ("Weight (kg)", "weight"), ("Ready date", "ready_date"), ("Booking number", "bookingCounter")],
    },
}

PROMPT = """You write the shared map card for one screen of a forwarder portal (air freight).
The card has two views of the SAME facts:
- forwarder: the words a forwarder uses on screen (HAWB, chargeable weight, ETA, Download PDF), no code,
  no selectors, no event names.
- developer: code names, selectors, routes and event names (house_bill_no, chargeable_kg, eta_at,
  #download-pdf, $dead_click).
Use the word pairs in `words` to translate between the two views.

Rules:
- Use ONLY the facts in the JSON below. Do not invent numbers, features, causes, fixes, users or dates.
  If a fact is not in the data, leave it out. If there is nothing to say, say so plainly.
- Keep each field to one to three short sentences.
- what_happens: from `about`. how_people_use_it: from `usage` (visits, users, top paths, top clicks).
  where_it_breaks: from `problems` and `open_feedback`; mark problems listed in `fixed_today` as fixed.
- Copy `words`, `what_changed` and `sources` exactly as given.

Facts (JSON):
{facts}
"""


def build_all() -> list[ScreenCard]:
    con = labels.db(*REQUIRED_TABLES)
    try:
        events = posthog.load_events()
        now = posthog.now()
        signals = slack.list_signals()
        cards = []
        for screen, route in SCREENS.items():
            facts = _facts(con, screen, route, events, now, [s for s in signals if s["route"] == route])
            card = _card(facts)
            _save(con, card)
            cards.append(card)
        return cards
    finally:
        con.close()


def _facts(con, screen: str, route: str, events: list[dict], now: datetime, signals: list[dict]) -> dict:
    code = APP_JS.read_text()
    words = [(f, c) for f, c in ABOUT[screen]["words"] if c in code]
    usage = _usage(route, events, now)
    problems = _problems(route, events, now)
    ids = [s["id"] for s in signals]
    marks = ",".join("?" * len(ids))
    label_rows = [dict(r) for r in con.execute(
        f"SELECT signal_id, category, code_area, outcome, created_at FROM labels WHERE signal_id IN ({marks})", ids)]
    checks = [dict(r) for r in con.execute(
        "SELECT signal_id, route, element, before, after, verdict, ts FROM impact_checks "
        "WHERE route = ? ORDER BY ts", (route,))]
    replies = {sid: slack.list_replies(sid) for sid in ids}

    merged = {r["signal_id"]: r for r in label_rows if r["outcome"] in MERGED}
    latest_check = {c["signal_id"]: c for c in checks}  # ordered by ts: last one wins

    changed_fw, changed_dev, fixed_today = [], [], []
    for s in signals:
        if s["id"] not in merged:
            continue
        lab, chk = merged[s["id"]], latest_check.get(s["id"])
        dev = (f"{s['id']} ({lab['category']} · {lab['code_area']}): fix/{s['id']} merged with label "
               f"{lab['outcome']}.")
        if chk and chk["verdict"] != "skipped":
            dev += f" Impact check {chk['verdict']} on {chk['element']}: {chk['before']} -> {chk['after']} after the fix."
        changed_dev.append(dev)
        # the impact check names the element whose dead clicks/errors the fix removed
        trigger = chk and chk["verdict"] == "confirmed" and next(
            (p for p in problems if p["key"] == chk["element"] and p["text"]), None)
        if trigger:
            changed_fw.append(f"{trigger['text']} works again (fixed today after {s['user']}'s report, {s['id']}).")
            fixed_today.append(trigger["key"])
        else:
            changed_fw.append(f"Fixed today: \"{s['text']}\" ({s['id']}).")
        if replies[s["id"]]:
            changed_fw[-1] += f" We replied to {s['user']} in Slack."

    open_feedback = [{"id": s["id"], "source": s["source"], "user": s["user"], "text": s["text"]}
                     for s in signals if s["id"] not in merged]

    sources = [Source(kind="code", ref=str(APP_JS)),
               Source(kind="posthog", ref=f"$pageview/$autocapture on {route}, {usage['window']}")]
    sources += [Source(kind="posthog", ref=f"{p['event']} {p['key']} on {route}, last 24 h") for p in problems]
    sources += [Source(kind="slack", ref=f"{s['id']}: {s['text']}") for s in signals]
    sources += [Source(kind="label", ref=f"{r['signal_id']}: {r['outcome']}") for r in label_rows]
    sources += [Source(kind="pr", ref=f"fix/{sid}") for sid in merged]
    sources += [Source(kind="impact_check", ref=f"{c['signal_id']}: {c['verdict']} {c['before']} -> {c['after']}")
                for c in latest_check.values()]
    sources += [Source(kind="slack", ref=f"reply to {sid}: {r['text']}") for sid, rs in replies.items() for r in rs]

    return {
        "screen": screen, "route": route, "about": {k: ABOUT[screen][k] for k in ("forwarder", "developer")},
        "words": [{"forwarder_term": f, "code_name": c} for f, c in words],
        "usage": usage, "problems": problems, "fixed_today": fixed_today, "open_feedback": open_feedback,
        "what_changed": {"forwarder": " ".join(changed_fw) or "Nothing changed on this screen today.",
                         "developer": " ".join(changed_dev) or "No fixes merged for this screen today."},
        "sources": [s.model_dump() for s in sources],
    }


def _ts(e: dict) -> datetime:
    return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))


def _usage(route: str, events: list[dict], now: datetime) -> dict:
    """Pageviews, users, top 3 paths through this screen and top 3 clicked elements over the dataset's 72 h."""
    since = now - timedelta(hours=72)
    recent = [e for e in events if _ts(e) > since]
    views = [e for e in recent if e["event"] == "$pageview" and posthog.route_of(e["$current_url"]) == route]
    by_session = defaultdict(list)
    for e in sorted((e for e in recent if e["event"] == "$pageview"), key=_ts):
        r = posthog.route_of(e["$current_url"])
        if not by_session[e["$session_id"]] or by_session[e["$session_id"]][-1] != r:
            by_session[e["$session_id"]].append(r)
    paths = Counter(f"{a} -> {b}" for seq in by_session.values() for a, b in zip(seq, seq[1:]) if route in (a, b))
    clicks = Counter()
    texts = defaultdict(Counter)
    for e in recent:
        if e["event"] == "$autocapture" and posthog.route_of(e["$current_url"]) == route:
            sel, text = posthog.selector_of(e["$elements_chain"])
            clicks[sel] += 1
            texts[sel][text] += 1
    return {
        "window": "last 72 h",
        "visits": len(views),
        "users": len({e["distinct_id"] for e in views}),
        "top_paths": [{"path": p, "count": n} for p, n in paths.most_common(3)],
        "top_clicks": [{"selector": s, "shown": _shown(texts[s]) or s, "count": n} for s, n in clicks.most_common(3)],
    }


def _problems(route: str, events: list[dict], now: datetime) -> list[dict]:
    """Rage clicks, dead clicks and exceptions of the last 24 h, grouped by element (or error), biggest first."""
    since = now - timedelta(hours=24)
    groups = defaultdict(list)
    for e in events:
        if e["event"] in PROBLEM_EVENTS and _ts(e) > since and posthog.route_of(e["$current_url"]) == route:
            key = (f"{e['$exception_type']}: {e['$exception_message']}" if e["event"] == "$exception"
                   else posthog.selector_of(e["$elements_chain"])[0])
            groups[(e["event"], key)].append(e)
    out = []
    for (event, key), es in groups.items():
        if len(es) < MIN_GROUP_EVENTS:
            continue
        texts = Counter(posthog.selector_of(e["$elements_chain"])[1] for e in es if e["event"] != "$exception")
        out.append({"event": event, "key": key, "text": texts.most_common(1)[0][0] if texts else "",
                    "shown": _shown(texts), "events": len(es), "users": len({e["distinct_id"] for e in es})})
    # dead clicks before exceptions on ties: the clicked element is what both views can name
    return sorted(out, key=lambda p: (-p["events"] * p["users"], p["event"] == "$exception"))


def _shown(texts: Counter) -> str:
    """How a forwarder sees the element: its text, or 'values like ...' when every row shows another value."""
    named = [(t, n) for t, n in texts.most_common() if t]
    if not named:
        return ""
    return f"'{named[0][0]}'" if len(named) == 1 else f"values like '{named[0][0]}'"


def _card(facts: dict) -> ScreenCard:
    words = [Word(**w) for w in facts["words"]]
    sources = [Source(**s) for s in facts["sources"]]
    changed = facts["what_changed"]
    if config.settings.OFFLINE:
        return _python_card(facts, words, sources)
    card = llm("screen_card", ScreenCard, PROMPT.format(facts=json.dumps(facts, indent=1)))
    # Exact lists and change log come from Python, never from the model.
    card.screen, card.route, card.sources = facts["screen"], facts["route"], sources
    card.forwarder.words = card.developer.words = words
    card.forwarder.what_changed, card.developer.what_changed = changed["forwarder"], changed["developer"]
    return card


def _python_card(facts: dict, words: list[Word], sources: list[Source]) -> ScreenCard:
    u, problems = facts["usage"], facts["problems"]
    paths = ", ".join(f"{p['path']} ({p['count']})" for p in u["top_paths"]) or "none"
    clicks_fw = ", ".join(f"{c['shown']} ({c['count']})" for c in u["top_clicks"]) or "none"
    clicks_dev = ", ".join(f"{c['selector']} ({c['count']})" for c in u["top_clicks"]) or "none"
    verbs = {"$rageclick": "clicked {t} again and again", "$dead_click": "clicked {t} and nothing happened"}

    breaks_fw, breaks_dev = [], []
    for p in problems:
        fixed = " (fixed today)" if p["key"] in facts["fixed_today"] else ""
        breaks_dev.append(f"{p['event']} {p['key']}: {p['events']} events, {p['users']} users, last 24 h{fixed}.")
        if p["event"] in verbs:  # errors are invisible to forwarders; their click is reported instead
            what = verbs[p["event"]].format(t=p["shown"] or "it")
            breaks_fw.append(f"{p['users']} people {what} ({p['events']} times in the last 24 h){fixed}.")
    for f in facts["open_feedback"]:
        if f["source"] == "feedback":  # heatmap signals are already in `problems`
            breaks_fw.append(f"{f['user']} reported: \"{f['text']}\" ({f['id']}).")
        breaks_dev.append(f"Open {f['source']} signal {f['id']}: \"{f['text']}\".")

    return ScreenCard(
        screen=facts["screen"], route=facts["route"], sources=sources,
        forwarder=ScreenView(
            what_happens=facts["about"]["forwarder"],
            how_people_use_it=f"{u['visits']} visits by {u['users']} people in the {u['window']}. "
                              f"Most clicked: {clicks_fw}.",
            words=words,
            where_it_breaks=" ".join(breaks_fw) or "No reported problems.",
            what_changed=facts["what_changed"]["forwarder"],
        ),
        developer=ScreenView(
            what_happens=facts["about"]["developer"],
            how_people_use_it=f"{u['visits']} $pageview on {facts['route']} from {u['users']} distinct_id, "
                              f"{u['window']}. Top paths: {paths}. Top $autocapture: {clicks_dev}.",
            words=words,
            where_it_breaks=" ".join(breaks_dev) or "No rage/dead click or exception groups and no open signals.",
            what_changed=facts["what_changed"]["developer"],
        ),
    )


def _save(con, card: ScreenCard) -> None:
    """Upsert into screen_cards only when the card changed; bump version."""
    new = card.model_dump_json()
    row = con.execute("SELECT card, version FROM screen_cards WHERE screen = ?", (card.screen,)).fetchone()
    if row and row["card"] == new:
        return
    version = row["version"] + 1 if row else 1
    con.execute("INSERT INTO screen_cards (screen, route, card, version, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(screen) DO UPDATE SET route = excluded.route, card = excluded.card, "
                "version = excluded.version, updated_at = excluded.updated_at",
                (card.screen, card.route, new, version, datetime.now(timezone.utc).isoformat()))
    con.commit()


if __name__ == "__main__":
    for c in build_all():
        print(c.model_dump_json(indent=1))

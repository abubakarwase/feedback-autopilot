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
        "forwarder": ["Lists your active house bills (HAWB), one row each",
                      "Shows origin, destination, weight and chargeable weight",
                      "Shows status and ETA per HAWB"],
        "developer": ["renderShipments(view) renders SHIPMENTS into table#shipments-table",
                      "Default route: #/shipments"],
        "elements": {"td.eta-cell span": "the ETA date", "table#shipments-table td": "shipment rows",
                     "a.nav-link": "menu links"},
        "words": [("HAWB", "house_bill_no"), ("Origin", "origin"), ("Destination", "destination"),
                  ("Weight", "weight_kg"), ("Chargeable weight", "chargeable_kg"), ("Status", "status"),
                  ("ETA", "eta_at")],
    },
    "invoice": {
        "forwarder": ["Shows one invoice: customer, HAWB, issue and due date",
                      "Lists the line items and the total",
                      "Download PDF saves the invoice as a file"],
        "developer": ["renderInvoice(view, id) renders INVOICES[id] on #/invoices/:id",
                      "button#download-pdf calls downloadInvoicePdf(inv)",
                      "downloadInvoicePdf builds the file with buildInvoicePdf(inv)"],
        "elements": {"button#download-pdf": "Download PDF", "table#invoice-lines td": "invoice line items",
                     "button#feedback-button": "the Feedback button", "a.nav-link": "menu links"},
        "words": [("Invoice number", "id"), ("Customer", "customer"), ("HAWB", "house_bill_no"),
                  ("Issued", "issued"), ("Due", "due"), ("Line items", "lines"), ("Total", "invoice-total"),
                  ("Download PDF", "downloadInvoicePdf")],
    },
    "booking": {
        "forwarder": ["Request an air freight pickup",
                      "Enter origin, destination, pieces, weight (kg) and ready date",
                      "You get a booking number right away"],
        "developer": ["renderBooking(view) renders form#booking-form on #/booking",
                      "Submit builds a BKG-2026-* number in the browser (bookingCounter)",
                      "No backend call; shows #booking-confirmation"],
        "elements": {"form#booking-form input": "booking form fields", "a.nav-link": "menu links"},
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

Format (every text field, both views):
- 2 to 4 bullet lines, each starting with "- ", separated by newlines. No other text.
- Each line at most 14 words. Concrete numbers (visits, users, counts, ids). No filler.
- A fact appears in one field only; never repeat it in another field.
- forwarder lines: forwarder words only (use `forwarder_name` for clicked elements, user `name` for people);
  never a selector, route, file name or event name.
- developer lines: the same facts in code words (selectors, routes, event names, signal ids, distinct_id).

Rules:
- Use ONLY the facts in the JSON below. Never invent numbers, features, causes, fixes, workarounds,
  tests, files, users or dates. If a fact is not in the data, leave it out.
- what_happens: from `about`. how_people_use_it: from `usage` (visits, users, top paths, top clicks).
- where_it_breaks: open problems from `problems` (skip any whose key is in `fixed_today`: they are in
  what_changed) and `open_feedback`, with counts. Forwarders do not see $exception: only developer lines
  name it. If nothing is open, write exactly "- No open problems".
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
    for item in usage["top_clicks"] + problems:  # how a forwarder names the element; "" = not nameable
        selector = item["selector"] if "selector" in item else item["key"]
        item["forwarder_name"] = ABOUT[screen]["elements"].get(selector, item["shown"])
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
        name = _name(s["user"])
        changed_dev.append(f"- {s['id']} merged: PR fix/{s['id']}, label {lab['outcome']} "
                           f"({lab['category']}, {lab['code_area']})")
        # the impact check names the element whose dead clicks/errors the fix removed
        trigger = chk and chk["verdict"] == "confirmed" and next(
            (p for p in problems if p["key"] == chk["element"] and p["text"]), None)
        if trigger:
            changed_fw.append(f"- Fixed today: {trigger['text']} works again (reported by {name}, {s['id']})")
            changed_dev.append(f"- Impact check confirmed: {trigger['event']} on {chk['element']} "
                               f"{chk['before']} -> {chk['after']}")
            fixed_today.append(trigger["key"])
        else:
            changed_fw.append(f"- Fixed today: \"{_clip(s['text'], 8)}\" (reported by {name}, {s['id']})")
            if chk and chk["verdict"] != "skipped":
                changed_dev.append(f"- Impact check {chk['verdict']}: {chk['element']} {chk['before']} -> {chk['after']}")
        if replies[s["id"]]:
            changed_fw.append(f"- We replied to {name} in Slack")
            changed_dev.append(f"- Slack reply posted in the {s['id']} thread")

    open_feedback = [{"id": s["id"], "source": s["source"], "user": s["user"], "name": _name(s["user"]),
                      "text": s["text"], "element": s["element"]}
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
        "screen": screen, "route": route,
        "about": {k: "\n".join(f"- {line}" for line in ABOUT[screen][k]) for k in ("forwarder", "developer")},
        "words": [{"forwarder_term": f, "code_name": c} for f, c in words],
        "usage": usage, "problems": problems, "fixed_today": fixed_today, "open_feedback": open_feedback,
        "what_changed": {"forwarder": "\n".join(_cap(changed_fw)) or "- No changes this week",
                         "developer": "\n".join(_cap(changed_dev)) or "- No changes this week"},
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


def _name(user: str) -> str:
    """'u-anna' -> 'Anna'."""
    return user.removeprefix("u-").capitalize()


def _clip(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n]) + ("..." if len(words) > n else "")


def _cap(lines: list[str]) -> list[str]:
    """At most 4 bullets; the rest are counted, they stay in the sources."""
    return lines if len(lines) <= 4 else lines[:3] + [f"- {len(lines) - 3} more, see sources"]


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
    u, route = facts["usage"], facts["route"]
    screen_of = {r: n for n, r in SCREENS.items()}
    incoming = [p for p in u["top_paths"] if p["path"].endswith(f"-> {route}")][:2]
    clicks_fw = [f"{c['forwarder_name']} ({c['count']})" for c in u["top_clicks"] if c["forwarder_name"]]
    use_fw = [f"- {u['visits']} visits by {u['users']} people in the {u['window']}"]
    use_dev = [f"- {u['visits']} $pageview from {u['users']} distinct_id, {u['window']}"]
    if incoming:
        use_fw.append("- Usually opened from: " + ", ".join(
            f"{screen_of[p['path'].split(' -> ')[0]]} ({p['count']})" for p in incoming))
    if u["top_paths"]:
        use_dev.append("- Top paths: " + ", ".join(f"{p['path']} ({p['count']})" for p in u["top_paths"][:2]))
    if clicks_fw:
        use_fw.append("- Top clicks: " + ", ".join(clicks_fw))
    if u["top_clicks"]:
        use_dev.append("- Top $autocapture: " + ", ".join(f"{c['selector']} ({c['count']})" for c in u["top_clicks"]))

    verbs = {"$rageclick": "clicked {t} again and again", "$dead_click": "clicked {t}, nothing happened"}
    heatmap_ids = {f["element"]: f["id"] for f in facts["open_feedback"] if f["source"] == "heatmap"}
    breaks_fw, breaks_dev = [], []
    for p in facts["problems"]:
        if p["key"] in facts["fixed_today"]:  # listed under what_changed
            continue
        sid = f" ({heatmap_ids[p['key']]})" if p["key"] in heatmap_ids else ""
        breaks_dev.append(f"- {p['event']} {_clip(p['key'], 6)}: {p['events']} events, {p['users']} users, 24 h{sid}")
        if p["event"] in verbs:  # errors are invisible to forwarders; their click is reported instead
            what = verbs[p["event"]].format(t=p["forwarder_name"] or "it")
            breaks_fw.append(f"- {p['users']} people {what} ({p['events']} times, last 24 h)")
    problem_keys = {p["key"] for p in facts["problems"]}
    for f in facts["open_feedback"]:
        if f["source"] == "feedback":
            breaks_fw.append(f"- {f['name']} reported: \"{_clip(f['text'], 8)}\" ({f['id']})")
            breaks_dev.append(f"- {f['id']} open: \"{_clip(f['text'], 8)}\"")
        elif f["element"] not in problem_keys:  # heatmap signals already named on their problem line
            breaks_dev.append(f"- {f['id']} open {f['source']} signal on {f['element']}")

    return ScreenCard(
        screen=facts["screen"], route=route, sources=sources,
        forwarder=ScreenView(
            what_happens=facts["about"]["forwarder"],
            how_people_use_it="\n".join(use_fw),
            words=words,
            where_it_breaks="\n".join(_cap(breaks_fw)) or "- No open problems",
            what_changed=facts["what_changed"]["forwarder"],
        ),
        developer=ScreenView(
            what_happens=facts["about"]["developer"],
            how_people_use_it="\n".join(use_dev),
            words=words,
            where_it_breaks="\n".join(_cap(breaks_dev)) or "- No open problems",
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

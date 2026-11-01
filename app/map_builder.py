"""Shared map: one ScreenCard per screen, built from PostHog usage, signals, labels and impact checks.

Usage numbers are computed in Python. The LLM only writes the prose (what happens, how people use it)
from those facts; words, where it breaks, what changed and sources are always the Python values, so
the card never claims a fix, a term or a source that is not in the data. With OFFLINE=1 the whole card
is built in Python.

where_it_breaks / what_changed lines follow the status tag contract shared with ui/map_view.py:
"- [tag] text", tag in TAGS. PR and issue state come from the GitHub mock DB
(GITHUB_ROOT / .github_mock.sqlite); a missing file means no PRs or issues yet.
"""
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.llm import llm
from app.schemas import ScreenCard, ScreenView, Source, Word
from app.tools import github, labels, posthog, slack

SCREENS = {"shipments": "/shipments", "invoice": "/invoices/:id", "booking": "/booking"}
APP_JS = Path("target-app/app.js")
PROBLEM_EVENTS = ("$rageclick", "$dead_click", "$exception")
MERGED = ("merged_unchanged", "edited")
MIN_GROUP_EVENTS = 5  # same threshold as the impact check: smaller groups are noise
REQUIRED_TABLES = ("signals", "replies", "labels", "impact_checks", "screen_cards", "trust")
GITHUB_ROOT = Path(".")  # the GitHub mock DB lives at GITHUB_ROOT / github.DB_NAME
TAGS = ("open", "in review", "to product", "triaged", "fixed · auto", "fixed · reviewed", "rejected",
        "duplicate", "confirmed")
MAX_LINES = 5
KIND = {"copy": "text fix", "bug": "bug fix", "ux": "layout fix"}  # forwarder words for a category

# What each screen is, read from target-app/app.js. Words are (forwarder term, code name).
ABOUT = {
    "shipments": {
        "forwarder": ["See where each shipment (HAWB) is and when it arrives",
                      "Check weights and chargeable weight before billing"],
        "developer": ["Route #/shipments (default): renderShipments(view)",
                      "Renders SHIPMENTS into table#shipments-table, ETA in td.eta-cell"],
        "elements": {"td.eta-cell span": "the ETA date", "table#shipments-table td": "shipment rows",
                     "a.nav-link": "menu links"},
        "words": [("HAWB", "house_bill_no"), ("Origin", "origin"), ("Destination", "destination"),
                  ("Weight", "weight_kg"), ("Chargeable weight", "chargeable_kg"), ("Status", "status"),
                  ("ETA", "eta_at")],
    },
    "invoice": {
        "forwarder": ["Check an invoice before it goes to the customer",
                      "Download the PDF to email it"],
        "developer": ["Route #/invoices/:id: renderInvoice(view, id) renders INVOICES[id]",
                      "button#download-pdf calls downloadInvoicePdf(inv)",
                      "downloadInvoicePdf builds the file with buildInvoicePdf(inv)"],
        "elements": {"button#download-pdf": "Download PDF", "table#invoice-lines td": "invoice line items",
                     "button#feedback-button": "the Feedback button", "a.nav-link": "menu links"},
        "words": [("Invoice number", "id"), ("Customer", "customer"), ("HAWB", "house_bill_no"),
                  ("Issued", "issued"), ("Due", "due"), ("Line items", "lines"), ("Total", "invoice-total"),
                  ("Download PDF", "downloadInvoicePdf")],
    },
    "booking": {
        "forwarder": ["Book a new shipment: origin, destination, pieces, weight",
                      "Get a booking number right away"],
        "developer": ["Route #/booking: renderBooking(view) renders form#booking-form",
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
- 2 to 3 bullet lines, each starting with "- ", separated by newlines. No other text.
- Each line at most 14 words. Concrete numbers (visits, users, counts, ids). No filler.
- A fact appears in one field only; never repeat it in another field.
- forwarder lines: forwarder words only (use `forwarder_name` for clicked elements, user `name` for people);
  never a selector, route, file name or event name.
- developer lines: the same facts in code words (selectors, routes, event names, signal ids, distinct_id).

Rules:
- Use ONLY the facts in the JSON below. Never invent numbers, features, causes, fixes, workarounds,
  tests, files, users or dates. If a fact is not in the data, leave it out.
- what_happens: from `about`; forwarder lines are tasks a forwarder does here, in their words.
- how_people_use_it: from `usage` (visits, users, top paths, top clicks), at most 3 lines.
- where_it_breaks and what_changed: write exactly "-" (they are filled in from the data).

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

    latest_check = {c["signal_id"]: c for c in checks}  # ordered by ts: last one wins
    prs, issues = _github(ids)
    trust = {(r["category"], r["code_area"]): dict(r) for r in con.execute("SELECT * FROM trust")}
    status = _status(ABOUT[screen]["elements"], signals, problems, label_rows, latest_check, replies, prs,
                     issues, trust)

    sources = [Source(kind="code", ref=str(APP_JS)),
               Source(kind="posthog", ref=f"$pageview/$autocapture on {route}, {usage['window']}")]
    sources += [Source(kind="posthog", ref=f"{p['event']} {p['key']} on {route}, last 24 h") for p in problems]
    sources += [Source(kind="slack", ref=f"{s['id']}: {s['text']}") for s in signals]
    sources += [Source(kind="label", ref=f"{r['signal_id']}: {r['outcome']}") for r in label_rows]
    sources += [Source(kind="pr", ref=f"#{pr['number']} {pr['branch']} {pr['state']}") for pr in prs.values()]
    sources += [Source(kind="issue", ref=f"#{i['number']} {i['title']}") for i in issues.values()]
    sources += [Source(kind="impact_check", ref=f"{c['signal_id']}: {c['verdict']} {c['before']} -> {c['after']}")
                for c in latest_check.values()]
    sources += [Source(kind="slack", ref=f"reply to {sid}: {r['text']}") for sid, rs in replies.items() for r in rs]

    return {
        "screen": screen, "route": route,
        "about": {k: "\n".join(f"- {line}" for line in ABOUT[screen][k]) for k in ("forwarder", "developer")},
        "words": [{"forwarder_term": f, "code_name": c} for f, c in words],
        "usage": usage, "problems": problems, "status": status,
        "sources": [s.model_dump() for s in sources],
    }


def _github(ids: list[str]) -> tuple[dict, dict]:
    """PRs (branch fix/<id>) and issues (title "[<id>] ...") per signal id; the latest one wins."""
    path = (GITHUB_ROOT / github.DB_NAME).resolve()
    if not path.exists():  # no PR or issue opened yet: a real state
        return {}, {}
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        prs = {r["branch"].removeprefix("fix/"): dict(r) for r in con.execute("SELECT * FROM prs ORDER BY number")}
        issues = {r["title"][1:r["title"].index("]")]: dict(r)
                  for r in con.execute("SELECT * FROM issues ORDER BY number")
                  if r["title"].startswith("[") and "]" in r["title"]}
    finally:
        con.close()
    return {k: v for k, v in prs.items() if k in ids}, {k: v for k, v in issues.items() if k in ids}


def _hotspot(p: dict) -> tuple[str, str]:
    """A PostHog problem group in forwarder words ("" for errors: forwarders don't see them) and developer words."""
    what = p["forwarder_name"] or "it"
    fw = {"$rageclick": f"{p['users']} people clicked {what} again and again ({p['events']} times)",
          "$dead_click": f"{p['users']} people clicked {what} {p['events']} times, nothing happened"}.get(p["event"], "")
    return fw, f"{p['event']} {_clip(p['key'], 6)}: {p['events']} events, {p['users']} users, 24 h"


def _trust_dev(t: dict | None) -> str:
    if t is None:
        return "no track record"
    pct = f", {round(100 * t['merged_unchanged'] / t['cases'])}% unchanged" if t["cases"] else ""
    return f"{t['cases']} cases{pct}, {t['reverts']} reverts"


def _status(elements: dict, signals: list[dict], problems: list[dict], label_rows: list[dict], checks: dict,
            replies: dict, prs: dict, issues: dict, trust: dict) -> dict:
    """where_it_breaks / what_changed lines ("- [tag] text") per view, from labels, PRs, issues and checks."""
    by_key = {p["key"]: p for p in problems}
    lab, triaged = {}, {}
    for r in sorted(label_rows, key=lambda r: r["created_at"] or ""):
        lab[r["signal_id"]] = r
        if r["outcome"] == "triaged_by_human":
            triaged[r["signal_id"]] = r
    cat_fw = {"bug": "a bug", "copy": "wrong text", "ux": "a layout problem", "feature_request": "a feature request",
              "how_to": "a how-to question"}
    breaks, changed, covered, confirmed = [], [], set(), False

    def add(out, tag, fw, dev):
        out.append((tag, f"- [{tag}] {fw}" if fw else "", f"- [{tag}] {dev}"))

    for s in signals:  # newest first
        sid, l, pr, iss, chk = s["id"], lab.get(s["id"]), prs.get(s["id"]), issues.get(s["id"]), checks.get(s["id"])
        name, quote = _name(s["user"]), f"\"{_clip(s['text'], 8)}\""
        cat, area = (l["category"], l["code_area"]) if l else (None, None)
        t = trust.get((cat, area))
        trusted = bool(t and t["trusted"])
        prn = f"PR #{pr['number']} " if pr else ""
        if s["source"] == "heatmap" and s["element"] in by_key:
            subj_fw, subj_dev = _hotspot(by_key[s["element"]])
            subj_dev = f"{sid} {subj_dev}"
        else:
            subj_fw, subj_dev = f"{name} reported {quote}", f"{sid} {quote}"
        covered.add(s["element"])
        dup = next((r["text"].removeprefix("Duplicate of ").strip() for r in replies[sid]
                    if r["text"].startswith("Duplicate of ")), None)
        outcome = l["outcome"] if l else None
        if dup:
            add(changed, "duplicate", f"{subj_fw}: same as an earlier report ({dup}), added there.",
                f"{sid} duplicate of {dup}; evidence linked there")
        elif outcome == "reverted" or (pr and pr["state"] == "reverted"):
            add(breaks, "open", f"{subj_fw}. The fix made things worse and was undone.",
                f"{subj_dev}: {prn}reverted after the impact check")
            add(changed, "rejected", f"The fix for {quote} was undone because things got worse.",
                f"{sid} {prn}reverted (impact check worse)")
        elif outcome == "rejected":
            add(breaks, "open", f"{subj_fw}. A developer rejected the proposed fix.",
                f"{subj_dev}: {prn}rejected in review")
            add(changed, "rejected", f"A developer rejected the proposed fix for {quote}, so it is still open.",
                f"{sid} {prn}rejected by developer (label rejected, {cat} · {area})")
        elif outcome in MERGED or (pr and pr["state"] == "merged"):
            by = pr["merged_by"] if pr else ("auto" if trusted and outcome == "merged_unchanged" else "developer")
            ok = chk is not None and chk["verdict"] == "confirmed"
            what = (f"{elements[chk['element']]} works again" if ok and chk["element"] in elements
                    else f"{name}'s report {quote} is fixed")
            told = f" We told {name} in Slack." if replies[sid] else ""
            kind = f"{cat} · {area}" if l else "unlabelled"
            if by == "auto":
                record = (f" ({t['cases']} past fixes, {'none' if not t['reverts'] else t['reverts']} undone)"
                          if t else "")
                add(changed, "fixed · auto",
                    f"{what}. Done automatically: this kind of {KIND.get(cat, 'fix')} has a clean record{record}.{told}",
                    f"{sid} {prn}auto-merged: {kind} trusted ({_trust_dev(t)}); gate all green")
            else:
                who = "edited and approved" if outcome == "edited" else "checked and approved"
                why = ", because this kind of fix isn't trusted yet" if not trusted else ""
                add(changed, "fixed · reviewed", f"{what}. A developer {who} the fix{why}.{told}",
                    f"{sid} {prn}merged by developer ({outcome or 'no label'}): {kind} "
                    f"{'trusted, gate asked for review' if trusted else 'not trusted'} ({_trust_dev(t)})")
            if ok:
                confirmed = True
                covered.add(chk["element"])
                event = by_key[chk["element"]]["event"] if chk["element"] in by_key else ""
                noun = "failed clicks" if event in ("$dead_click", "$rageclick") else "problems"
                add(changed, "confirmed", f"Since the fix: {chk['before']} {noun} -> {chk['after']}",
                    f"{sid} impact check: {event + ' on ' if event else ''}{chk['element']} "
                    f"{chk['before']} -> {chk['after']}")
        elif pr and pr["state"] == "needs_review":
            why = " (not trusted yet)" if l and not trusted else ""
            add(breaks, "in review", f"{subj_fw}. A fix is waiting for a developer.",
                f"{subj_dev}: {prn}needs_review")
            add(changed, "in review", f"Fix ready for {quote}, waiting for a developer{why}.",
                f"{sid} {prn}waiting for review" + (f": {cat} · {area} not trusted ({_trust_dev(t)})" if why else ""))
        elif iss:
            if "tests_pass_after" in (iss["body"] or ""):  # open_issue after failed fix attempts: body is the FixAttempt
                add(breaks, "open", f"{subj_fw}. The automatic fix failed its tests, so a developer will look at it "
                    f"(issue #{iss['number']}).", f"{subj_dev}: fix attempts failed verify -> issue #{iss['number']}")
                continue
            add(breaks, "to product", f"{subj_fw}. Sent to the product team (issue #{iss['number']}).",
                f"{subj_dev}: issue #{iss['number']}")
            if sid in triaged:
                tr = triaged[sid]
                add(changed, "triaged", f"{name}'s report {quote} was too vague for the agent, so a developer sorted "
                    f"it as {cat_fw.get(tr['category'], tr['category'])} and sent it to product (issue #{iss['number']}).",
                    f"{sid} low confidence after retries; developer labelled {tr['category']} · "
                    f"{tr['code_area']} -> issue #{iss['number']}")
            else:
                short = (f"Clicks on {by_key[s['element']]['forwarder_name']}"
                         if s["source"] == "heatmap" and s["element"] in by_key else f"{name}'s report {quote}")
                add(changed, "to product", f"{short}: the agent judged this not a small code fix, so it went to the "
                    f"product team (issue #{iss['number']}).", f"{sid} not agent-fixable -> issue #{iss['number']}")
        elif outcome == "answered_from_card":
            changed.append(("", f"- {name}'s question was answered from this card in Slack",
                            f"- {sid} how_to answered from the screen card"))
        else:
            if sid in triaged:
                tr = triaged[sid]
                add(changed, "triaged", f"A vague report ({quote}) was sorted by a developer: "
                    f"{cat_fw.get(tr['category'], tr['category'])}.",
                    f"{sid} developer labelled {tr['category']} · {tr['code_area']}")
            add(breaks, "open", subj_fw, f"{subj_dev}: open, no PR or issue")

    for p in problems:  # hotspots no signal covers yet
        # the impact check also watches the screen's errors, so a confirmed fix covers them
        if p["key"] in covered or (confirmed and p["event"] == "$exception"):
            continue
        add(breaks, "open", *_hotspot(p))

    order = {"open": 0, "in review": 1, "triaged": 2, "to product": 2}
    breaks.sort(key=lambda b: order[b[0]])
    view = lambda rows, i, empty: "\n".join(_cap([r[i] for r in rows if r[i]])) or empty
    return {"forwarder": {"where_it_breaks": view(breaks, 1, "- Nothing broken right now"),
                          "what_changed": view(changed, 1, "- No changes yet")},
            "developer": {"where_it_breaks": view(breaks, 2, "- Nothing broken right now"),
                          "what_changed": view(changed, 2, "- No changes yet")}}


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
    """At most MAX_LINES bullets; the rest are counted, they stay in the sources."""
    if len(lines) <= MAX_LINES:
        return lines
    return lines[:MAX_LINES - 1] + [f"- {len(lines) - MAX_LINES + 1} more, see sources"]


def _shown(texts: Counter) -> str:
    """How a forwarder sees the element: its text, or 'values like ...' when every row shows another value."""
    named = [(t, n) for t, n in texts.most_common() if t]
    if not named:
        return ""
    return f"'{named[0][0]}'" if len(named) == 1 else f"values like '{named[0][0]}'"


def _card(facts: dict) -> ScreenCard:
    words = [Word(**w) for w in facts["words"]]
    sources = [Source(**s) for s in facts["sources"]]
    if config.settings.OFFLINE:
        return _python_card(facts, words, sources)
    card = llm("screen_card", ScreenCard, PROMPT.format(facts=json.dumps(facts, indent=1)))
    # Exact lists, problems and change log come from Python, never from the model.
    card.screen, card.route, card.sources = facts["screen"], facts["route"], sources
    card.forwarder.words = card.developer.words = words
    for v in ("forwarder", "developer"):
        view = getattr(card, v)
        view.what_happens = facts["about"][v]
        view.how_people_use_it = "\n".join(view.how_people_use_it.splitlines()[:3])
        view.where_it_breaks = facts["status"][v]["where_it_breaks"]
        view.what_changed = facts["status"][v]["what_changed"]
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

    return ScreenCard(
        screen=facts["screen"], route=route, sources=sources,
        forwarder=ScreenView(
            what_happens=facts["about"]["forwarder"],
            how_people_use_it="\n".join(use_fw),
            words=words,
            where_it_breaks=facts["status"]["forwarder"]["where_it_breaks"],
            what_changed=facts["status"]["forwarder"]["what_changed"],
        ),
        developer=ScreenView(
            what_happens=facts["about"]["developer"],
            how_people_use_it="\n".join(use_dev),
            words=words,
            where_it_breaks=facts["status"]["developer"]["where_it_breaks"],
            what_changed=facts["status"]["developer"]["what_changed"],
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

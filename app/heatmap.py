"""Nightly heatmap scan and post-merge impact check (plain Python, no LLM).

scan(): group the last 24 h of $rageclick / $dead_click / $exception by route + element
(exceptions join the click group they happened next to, else they group by error message),
score users x events against the previous 7 days, and insert the top new groups as
heatmap signals. impact_check(): compare the group that triggered a merged feedback fix
before (main set, last 24 h) vs after (after-fix set).
"""
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from app.tools import labels, posthog
from app.tools.labels import is_duplicate

PROBLEM_EVENTS = ("$rageclick", "$dead_click", "$exception")
COUNT_KEY = {"$rageclick": "rage_clicks", "$dead_click": "dead_clicks", "$exception": "exceptions"}
ATTACH_WINDOW = timedelta(seconds=5)  # an exception this close to a click in the same session belongs to it
MIN_EVENTS, MIN_USERS = 5, 3
PREV_DAYS = 7
RISING_FACTOR = 2.0  # last 24 h vs the previous daily average
TRIGGER_MIN = 5  # dead clicks + exceptions for a group to count as the trigger of a fix
CONFIRMED_DROP = 0.8


def _ts(e: dict) -> datetime:
    return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))


def group_events(events: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Group flat PostHog problem events by (route, element selector, error).

    Exceptions within ATTACH_WINDOW of a dead/rage click in the same session join that
    click's group (error key ""); other exceptions group by route + error message.
    """
    problems = [e for e in events if e["event"] in PROBLEM_EVENTS]
    clicks = defaultdict(list)
    for e in problems:
        if e["event"] != "$exception":
            clicks[e["$session_id"]].append(e)

    raw: dict[tuple[str, str, str], dict] = {}
    for e in problems:
        route = posthog.route_of(e["$current_url"])
        error = ""
        if e["event"] == "$exception":
            error = f"{e['$exception_type']}: {e['$exception_message']}"
            near = [c for c in clicks[e["$session_id"]] if abs(_ts(c) - _ts(e)) <= ATTACH_WINDOW]
            element, text = posthog.selector_of(near[0]["$elements_chain"]) if near else ("", "")
            key = (route, element, "" if near else error)
        else:
            element, text = posthog.selector_of(e["$elements_chain"])
            key = (route, element, "")
        g = raw.setdefault(key, {"route": route, "element": element, "page_url": e["$current_url"],
                                 "texts": Counter(), "users": set(), "sessions": [], "errors": Counter(),
                                 "events": 0, "rage_clicks": 0, "dead_clicks": 0, "exceptions": 0})
        if text:
            g["texts"][text] += 1
        if error:
            g["errors"][error] += 1
        g["users"].add(e["distinct_id"])
        if e["$session_id"] not in g["sessions"]:
            g["sessions"].append(e["$session_id"])
        g["events"] += 1
        g[COUNT_KEY[e["event"]]] += 1

    return {key: {"route": g["route"], "element": g["element"], "page_url": g["page_url"],
                  "text": g["texts"].most_common(1)[0][0] if g["texts"] else "",
                  "error": g["errors"].most_common(1)[0][0] if g["errors"] else "",
                  "users": len(g["users"]), "events": g["events"], "rage_clicks": g["rage_clicks"],
                  "dead_clicks": g["dead_clicks"], "exceptions": g["exceptions"],
                  "sample_session": g["sessions"][0]}
            for key, g in raw.items()}


def score_groups(groups_24h: dict, groups_prev: dict) -> list[dict]:
    """Score = users x events; new and rising groups first, then by score.

    Groups below MIN_EVENTS / MIN_USERS are dropped (single noise clicks are always "new").
    """
    out = []
    for key, g in groups_24h.items():
        if g["events"] < MIN_EVENTS or g["users"] < MIN_USERS:
            continue
        prev = groups_prev.get(key)
        prev_events = prev["events"] if prev else 0
        if prev is None:
            trend = "new"
        elif g["events"] > RISING_FACTOR * prev_events / PREV_DAYS:
            trend = "rising"
        else:
            trend = "known"
        out.append({**g, "score": g["users"] * g["events"], "trend": trend, "prev_events": prev_events})
    return sorted(out, key=lambda g: (g["trend"] == "known", -g["score"]))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def signal_id_of(group: dict) -> str:
    """HM-<screen>-<element slug>, e.g. HM-shipments-eta-cell, HM-invoices-download-pdf."""
    screen = group["route"].strip("/").split("/")[0]
    anchor = group["element"].split(" ")[0]
    if "#" in anchor:
        name = anchor.split("#", 1)[1]
    elif "." in anchor:
        name = anchor.split(".")[1]
    elif anchor:
        name = anchor
    else:
        name = group["error"].split(":", 1)[0]
    return f"HM-{_slug(screen)}-{_slug(name)}"


def describe(group: dict, signal_id: str) -> str:
    """Human-readable signal text with counts, visible text, selector and route."""
    counts = ", ".join(f"{group[k]} {label}" for k, label in
                       (("rage_clicks", "rage clicks"), ("dead_clicks", "dead clicks"), ("exceptions", "errors"))
                       if group[k])
    if group["element"]:
        name = signal_id.split("-", 2)[2].replace("-", " ")
        shown = f" '{group['text']}'" if group["text"] else ""
        where = f" on {name}{shown} ({group['element']})"
    else:
        where = ""
    error = f"; error {group['error']}" if group["error"] else ""
    return f"{counts} by {group['users']} users{where} on {group['route']}{error}"


def _windows(events: list[dict], end: datetime) -> tuple[list[dict], list[dict]]:
    day = end - timedelta(hours=24)
    start = day - timedelta(days=PREV_DAYS)
    last = [e for e in events if day < _ts(e) <= end]
    prev = [e for e in events if start < _ts(e) <= day]
    return last, prev


def scan(top_n: int = 3) -> list[dict]:
    """Insert the top N new heatmap groups of the last 24 h as signals; return them."""
    last, prev = _windows(posthog.load_events(), posthog.now())
    ranked = score_groups(group_events(last), group_events(prev))

    con = labels.db("signals")
    try:
        existing = [dict(r) for r in con.execute("SELECT * FROM signals")]
        new = []
        with con:
            for g in ranked:
                if len(new) == top_n:
                    break
                sid = signal_id_of(g)
                sig = {"id": sid, "source": "heatmap", "text": describe(g, sid), "user": "", "channel": "",
                       "file": "", "page_url": g["page_url"], "route": g["route"], "element": g["element"],
                       "stats": g, "ts": time.time(), "created_at": datetime.now(timezone.utc).isoformat()}
                if any(s["id"] == sid or is_duplicate(sig, s) for s in existing):
                    continue
                con.execute("INSERT INTO signals (id, source, text, user, channel, file, page_url, route, "
                            "element, stats, ts, created_at) VALUES (:id, :source, :text, :user, :channel, "
                            ":file, :page_url, :route, :element, :stats, :ts, :created_at)",
                            {**sig, "stats": json.dumps(g)})
                existing.append(sig)
                new.append(sig)
    finally:
        con.close()
    return new


def _trigger_group(groups: dict, route: str) -> tuple[tuple, dict] | None:
    on_route = [(k, g) for k, g in groups.items()
                if g["route"] == route and g["dead_clicks"] + g["exceptions"] >= TRIGGER_MIN]
    if not on_route:
        return None
    return max(on_route, key=lambda kg: kg[1]["dead_clicks"] + kg[1]["exceptions"])


def verdict_of(before: int, after: int) -> str:
    if after == 0 or before - after >= CONFIRMED_DROP * before:
        return "confirmed"
    if after > before:
        return "worse"
    return "no_change"


def impact_check() -> list[dict]:
    """Before/after check for each feedback signal whose fix was merged today."""
    last, _ = _windows(posthog.load_events(), posthog.now())
    before_groups = group_events(last)
    after_groups = group_events(posthog.load_events(after_fix=True))
    today = datetime.now().date().isoformat()

    con = labels.db("signals", "labels", "impact_checks")
    try:
        signals = con.execute(
            "SELECT DISTINCT s.id, s.route FROM signals s JOIN labels l ON l.signal_id = s.id "
            "WHERE s.source = 'feedback' AND l.outcome IN ('merged_unchanged', 'edited') "
            "AND substr(l.created_at, 1, 10) = ? ORDER BY s.id", (today,)).fetchall()
        results = []
        with con:
            for s in signals:
                sid, route = s["id"], s["route"]
                hit = _trigger_group(before_groups, route)
                if hit is None:
                    row = {"signal_id": sid, "route": route, "element": "", "before": None, "after": None,
                           "verdict": "skipped",
                           "message": f"{sid} impact check skipped: no dead-click or error group on {route}"}
                else:
                    key, g = hit
                    metric, label = (("dead_clicks", "dead clicks") if g["dead_clicks"]
                                     else ("exceptions", "errors"))
                    before = g[metric]
                    after = after_groups[key][metric] if key in after_groups else 0
                    verdict = verdict_of(before, after)
                    head = {"confirmed": "fix confirmed", "worse": "got worse after the fix",
                            "no_change": "fix shows no clear effect"}[verdict]
                    row = {"signal_id": sid, "route": route, "element": g["element"], "before": before,
                           "after": after, "verdict": verdict,
                           "message": f"{sid} {head}: {label} {before} -> {after}"}
                row["ts"] = time.time()
                con.execute("DELETE FROM impact_checks WHERE signal_id = ?", (sid,))
                con.execute("INSERT INTO impact_checks (signal_id, route, element, before, after, verdict, ts) "
                            "VALUES (:signal_id, :route, :element, :before, :after, :verdict, :ts)", row)
                results.append(row)
    finally:
        con.close()
    return results

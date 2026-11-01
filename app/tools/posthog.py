"""PostHog tools (mocked: data/posthog_events.json, data/posthog_events_after_fix.json).

Events are returned flat: PostHog `properties` merged into the top level.
"Last N hours" is relative to now() = newest event of the main dataset, not the wall clock.
"""
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
MAIN_FILE = DATA_DIR / "posthog_events.json"
AFTER_FIX_FILE = DATA_DIR / "posthog_events_after_fix.json"
DEFAULT_ROUTE = "/shipments"  # target app falls back to #/shipments without a hash
PROBLEM_EVENTS = ("$dead_click", "$rageclick", "$exception")
_ID_SEGMENT = re.compile(r"^(?:[A-Z]+-\d+|\d+)$")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


@cache
def load_events(after_fix: bool = False) -> list[dict]:
    """All events of one dataset, flat, sorted by timestamp."""
    path = AFTER_FIX_FILE if after_fix else MAIN_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run: uv run python data/generate.py")
    raw = json.loads(path.read_text())
    flat = [{k: v for k, v in e.items() if k != "properties"} | e["properties"] for e in raw]
    return sorted(flat, key=lambda e: e["timestamp"])


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@cache
def now() -> datetime:
    """Newest timestamp of the main dataset."""
    return _parse_ts(load_events()[-1]["timestamp"])


def route_of(url_or_path: str) -> str:
    """Normalised route pattern: full href, hash path or pattern -> e.g. '/invoices/:id'."""
    if "#" in url_or_path:
        path = url_or_path.split("#", 1)[1]
    elif "://" in url_or_path:
        path = ""
    else:
        path = url_or_path
    path = path.split("?", 1)[0].strip("/")
    if not path:
        return DEFAULT_ROUTE
    return "/" + "/".join(":id" if _ID_SEGMENT.match(s) else s for s in path.split("/"))


def _since(hours: int, events: list[dict]) -> list[dict]:
    cutoff = now() - timedelta(hours=hours)
    return [e for e in events if _parse_ts(e["timestamp"]) >= cutoff]


def get_session_events(session_id: str) -> list[dict]:
    """Return all events of one session (main dataset, then after-fix)."""
    events = [e for e in load_events() + load_events(after_fix=True) if e["$session_id"] == session_id]
    if not events:
        raise KeyError(f"unknown PostHog session: {session_id}")
    return events


def query_events(event: str, url: str | None = None, hours: int = 24) -> list[dict]:
    """Return events of one type, optionally for one page, from the last `hours`."""
    events = [e for e in load_events() if e["event"] == event]
    if url is not None:
        route = route_of(url)
        events = [e for e in events if route_of(e["$current_url"]) == route]
    return _since(hours, events)


def _parse_element(el: str) -> tuple[str, list[str], dict]:
    head, _, attrs = el.partition(":")
    tag, *classes = head.split(".")
    return tag, classes, dict(_ATTR.findall(attrs))


def _css(tag: str, classes: list[str], attrs: dict) -> str:
    if "attr__id" in attrs:
        return f"{tag}#{attrs['attr__id']}"
    return ".".join([tag, *classes])


def selector_of(chain: str) -> tuple[str, str]:
    """(CSS-like selector, visible text) of the clicked element in a $elements_chain.

    Elements without id/class are anchored to their nearest ancestor that has one,
    e.g. 'td.eta-cell span'.
    """
    elements = [_parse_element(el) for el in chain.split(";")]
    tag, classes, attrs = elements[0]
    text = attrs.get("text", "")
    if "attr__id" in attrs or classes:
        return _css(tag, classes, attrs), text
    for a_tag, a_classes, a_attrs in elements[1:]:
        if "attr__id" in a_attrs or a_classes:
            return f"{_css(a_tag, a_classes, a_attrs)} {tag}", text
    return tag, text


def get_heatmap(url: str) -> dict:
    """Dead/rage-click and exception groups for a page over the last 24 h, by events desc."""
    route = route_of(url)
    events = _since(24, [e for e in load_events()
                         if e["event"] in PROBLEM_EVENTS and route_of(e["$current_url"]) == route])
    groups: dict[tuple[str, str], dict] = {}
    for e in events:
        if e["event"] == "$exception":
            selector, text = "", ""
            error = f"{e['$exception_type']}: {e['$exception_message']}"
        else:
            (selector, text), error = selector_of(e["$elements_chain"]), ""
        g = groups.setdefault((selector, error), {
            "selector": selector, "texts": Counter(), "users": set(), "events": 0,
            "dead_clicks": 0, "rage_clicks": 0, "exceptions": 0, "error": error})
        g["texts"][text] += 1
        g["users"].add(e["distinct_id"])
        g["events"] += 1
        key = {"$dead_click": "dead_clicks", "$rageclick": "rage_clicks", "$exception": "exceptions"}[e["event"]]
        g[key] += 1
    out = [{"selector": g["selector"], "text": g["texts"].most_common(1)[0][0], "users": len(g["users"]),
            "events": g["events"], "dead_clicks": g["dead_clicks"], "rage_clicks": g["rage_clicks"],
            "exceptions": g["exceptions"], "error": g["error"]} for g in groups.values()]
    return {"url": url, "route": route, "groups": sorted(out, key=lambda g: g["events"], reverse=True)}

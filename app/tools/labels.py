"""Label store tools (SQLite). Reach them from graph code only via app.mcp_client."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app import config
from app.tools import posthog

OUTCOMES = {"merged_unchanged", "edited", "rejected", "reverted", "triaged_by_human"}
# Words that carry no meaning for duplicate matching, incl. the heatmap signal text template.
STOPWORDS = {
    "this", "that", "does", "nothing", "with", "page", "these", "those", "column", "says",
    "button", "there", "here", "when", "what", "which", "have", "from", "into", "just", "some",
    "been", "were", "they", "them", "than", "then", "very", "really", "also", "only", "about",
    "should", "would", "could", "still", "doesn", "isn", "work", "works", "working",
    "rage", "dead", "click", "clicks", "users", "user", "exception", "exceptions", "error", "errors",
}
_NO_DB_HINT = 'run "uv run python data/generate.py"'


def db(*tables: str) -> sqlite3.Connection:
    """Open the shared SQLite DB (rows as sqlite3.Row). Raise if it or any of `tables` is missing."""
    path = Path(config.settings.DB_PATH)
    if not path.exists():
        raise FileNotFoundError(f"DB {path} not found: {_NO_DB_HINT}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = sorted(set(tables) - have)
    if missing:
        conn.close()
        raise RuntimeError(f"DB {path} is missing tables {missing}: {_NO_DB_HINT}")
    return conn


def _words(s: str, route: str) -> set[str]:
    # route words ("shipments", "invoices"/"invoice") match anything on that route, so ignore them
    route_words = {w for r in re.findall(r"[a-z]+", route.lower()) for w in (r, r.rstrip("s"))}
    return {w for w in re.findall(r"[a-z]+", s.lower())
            if len(w) >= 4 and w not in STOPWORDS and w not in route_words}


def _route(d: dict) -> str:
    return d["route"] if d.get("route") else posthog.route_of(d["page_url"])


def shared_words(a: dict, b: dict) -> set[str]:
    """Meaningful words shared by one signal's text and the other's text/element (same route only)."""
    route = _route(a)
    if route != _route(b):
        return set()
    a_text, b_text = _words(a["text"], route), _words(b["text"], route)
    a_el, b_el = _words(a.get("element") or "", route), _words(b.get("element") or "", route)
    return (a_text & (b_text | b_el)) | (b_text & a_el)


def is_duplicate(a: dict, b: dict) -> bool:
    """Same normalised route and a shared meaningful word; identical text on the same route is itself."""
    if a["text"].strip().lower() == b["text"].strip().lower():
        return False
    return bool(shared_words(a, b))


def find_duplicates(text: str, page_url: str = "") -> list[dict]:
    """Return open issues/signals similar to `text`."""
    me = {"text": text, "route": posthog.route_of(page_url) if page_url else ""}
    with closing(db("signals")) as conn:
        own = conn.execute("SELECT MAX(ts) FROM signals WHERE text = ? AND route = ?",
                           (text, me["route"])).fetchone()[0]
        rows = conn.execute("SELECT id, text, route, element FROM signals WHERE ? IS NULL OR ts < ? "
                            "ORDER BY ts", (own, own)).fetchall()
    return [{"signal_id": r["id"], "text": r["text"], "route": r["route"],
             "reason": f"same route {r['route']}, shared words: {', '.join(sorted(shared_words(me, dict(r))))}"}
            for r in rows if is_duplicate(me, dict(r))]


def _label(row: sqlite3.Row) -> dict:
    return {**dict(row), "triage_corrections": json.loads(row["triage_corrections"] or "{}")}


def get_similar_cases(category: str, code_area: str, limit: int = 5) -> list[dict]:
    """Return past labelled decisions for a category and code area."""
    with closing(db("labels")) as conn:
        rows = conn.execute("SELECT * FROM labels WHERE category = ? AND code_area = ? "
                            "ORDER BY created_at DESC, id DESC LIMIT ?",
                            (category, code_area, limit)).fetchall()
    return [_label(r) for r in rows]


def get_trust(category: str, code_area: str) -> dict:
    """Return the trust stats for category + code area."""
    with closing(db("trust")) as conn:
        row = conn.execute("SELECT * FROM trust WHERE category = ? AND code_area = ?",
                           (category, code_area)).fetchone()
    if row is None:
        return {"category": category, "code_area": code_area, "cases": 0, "merged_unchanged": 0,
                "reverts": 0, "trusted": False, "updated_at": None}
    return {**dict(row), "trusted": bool(row["trusted"])}


def save_label(signal_id: str, category: str, code_area: str, outcome: str,
               triage_corrections: dict | None = None) -> dict:
    """Store a label for a decided case."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}, expected one of {sorted(OUTCOMES)}")
    with closing(db("labels")) as conn, conn:
        cur = conn.execute(
            "INSERT INTO labels (signal_id, category, code_area, outcome, triage_corrections, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (signal_id, category, code_area, outcome, json.dumps(triage_corrections or {}),
             datetime.now().isoformat(timespec="seconds")))
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _label(row)

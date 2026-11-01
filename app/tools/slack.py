"""Slack tools (mocked: feedback lives in the SQLite signals/replies tables)."""
import json
import sqlite3
import time

from app.tools import labels

FIELDS = ("id", "text", "user", "channel", "file", "page_url", "ts", "source", "route", "element")


def _conn() -> sqlite3.Connection:
    con = labels.db()
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"signals", "replies"} - have
    if missing:
        con.close()
        raise RuntimeError(f"DB is missing tables {sorted(missing)}; run: uv run python data/generate.py")
    return con


def get_feedback(signal_id: str) -> dict:
    """Return the stored Slack feedback message for a signal id."""
    with _conn() as con:
        row = con.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown signal id: {signal_id}")
    return {k: row[k] for k in FIELDS}


def post_reply(signal_id: str, text: str) -> dict:
    """Post a reply to the feedback thread."""
    ts = time.time()
    with _conn() as con:
        if con.execute("SELECT 1 FROM signals WHERE id = ?", (signal_id,)).fetchone() is None:
            raise KeyError(f"unknown signal id: {signal_id}")
        cur = con.execute("INSERT INTO replies (signal_id, text, ts) VALUES (?, ?, ?)",
                          (signal_id, text, ts))
    return {"ok": True, "id": cur.lastrowid, "signal_id": signal_id, "text": text, "ts": ts}


def list_signals() -> list[dict]:
    """All signals, newest first; stats decoded from JSON."""
    with _conn() as con:
        rows = con.execute("SELECT * FROM signals ORDER BY ts DESC").fetchall()
    return [{**dict(r), "stats": json.loads(r["stats"] or "{}")} for r in rows]


def list_replies(signal_id: str | None = None) -> list[dict]:
    """Replies in thread order, optionally for one signal."""
    with _conn() as con:
        if signal_id is None:
            rows = con.execute("SELECT * FROM replies ORDER BY ts").fetchall()
        else:
            rows = con.execute("SELECT * FROM replies WHERE signal_id = ? ORDER BY ts",
                               (signal_id,)).fetchall()
    return [dict(r) for r in rows]

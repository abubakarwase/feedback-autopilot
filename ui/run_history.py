"""Run history: every "Run agent" / "Run again" / resume is kept as a row in the `runs` table (UI-owned).

Wiring into ui/streamlit_app.py (3 edits):
1. Import: `from ui import run_history`.
2. stream_into(graph_box, box, sid, events, kind): first line `run_id = run_history.start_run(sid, kind)`;
   inside the loop, after `trace.append(d)`, call `run_history.add_event(run_id, d)`;
   after the loop, `run_history.finish_run(run_id, _outcome(trace) if trace else "No steps")`.
   In right_column pass kind: `stream_into(..., run_signal(sid), "run")` and `stream_into(..., resume(sid, decision), "resume")`.
3. right_column, at the very end (after `for e in ss.traces[ss.selected]: render_event(e)`):
   `run_history.render_history(ss.selected)`. Optional: when `sid not in ss.traces` after a server restart,
   `ss.traces[sid] = run_history.latest_events(sid)` restores the current run from the DB.
A run that crashes mid-stream keeps ended_at NULL and shows as "did not finish".
"""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from app import config

ROOT = Path(__file__).resolve().parents[1]

_SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    signal_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('run', 'resume')),
    started_at REAL NOT NULL,
    ended_at REAL,
    outcome TEXT,
    events TEXT NOT NULL DEFAULT '[]'
)"""


def _connect() -> sqlite3.Connection:
    path = Path(config.settings.DB_PATH)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run: uv run python data/generate.py")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    return con


def start_run(signal_id: str, kind: str) -> int:
    if kind not in ("run", "resume"):
        raise ValueError(f"kind must be 'run' or 'resume', got {kind!r}")
    con = _connect()
    try:
        with con:
            cur = con.execute("INSERT INTO runs (signal_id, kind, started_at) VALUES (?, ?, ?)",
                              (signal_id, kind, time.time()))
        return cur.lastrowid
    finally:
        con.close()


def add_event(run_id: int, event: dict) -> None:
    con = _connect()
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")  # read-modify-write must not interleave with another writer
            row = con.execute("SELECT events FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run {run_id} not found")
            events = json.loads(row["events"])
            events.append(event)
            con.execute("UPDATE runs SET events = ? WHERE id = ?", (json.dumps(events), run_id))
    finally:
        con.close()


def finish_run(run_id: int, outcome: str) -> None:
    con = _connect()
    try:
        with con:
            cur = con.execute("UPDATE runs SET ended_at = ?, outcome = ? WHERE id = ?",
                              (time.time(), outcome, run_id))
        if cur.rowcount != 1:
            raise KeyError(f"run {run_id} not found")
    finally:
        con.close()


def list_runs(signal_id: str | None = None) -> list[dict]:
    """Newest first; `events` decoded to a list."""
    con = _connect()
    try:
        if signal_id is None:
            rows = con.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        else:
            rows = con.execute("SELECT * FROM runs WHERE signal_id = ? ORDER BY id DESC", (signal_id,)).fetchall()
    finally:
        con.close()
    return [{**dict(r), "events": json.loads(r["events"])} for r in rows]


def latest_events(signal_id: str) -> list[dict]:
    """Events of the latest 'run' plus every 'resume' after it, oldest first. [] if never run."""
    runs = list_runs(signal_id)
    out: list[list[dict]] = []
    for r in runs:  # newest first: collect resumes until the run that started them
        out.append(r["events"])
        if r["kind"] == "run":
            return [e for evs in reversed(out) for e in evs]
    if runs:
        raise RuntimeError(f"{signal_id}: resume rows without a preceding 'run' row")
    return []


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def _one_line(e: dict) -> str:
    o = e.get("output")
    if isinstance(o, dict):
        if o.get("route"):
            text = f"→ {o['route']}"
        else:
            text = ", ".join(f"{k}={v}" for k, v in o.items() if isinstance(v, (str, int, float, bool)))
    else:
        text = "" if o is None else str(o)
    p = f" · {e['p']:.0%} sure" if isinstance(e.get("p"), (int, float)) else ""
    return (text[:140] + "…" if len(text) > 140 else text) + p


def render_history(signal_id: str) -> None:
    runs = list_runs(signal_id)
    if not runs:
        return
    st.markdown(f"**Earlier runs ({len(runs)})**")
    for r in runs:
        kind = "Run" if r["kind"] == "run" else "Continued after review"
        outcome = r["outcome"] or ("did not finish" if r["ended_at"] is None else "")
        n = len(r["events"])
        with st.expander(f"{_when(r['started_at'])} · {kind} · {outcome} · {n} step{'s' if n != 1 else ''}"):
            if not r["events"]:
                st.caption("No steps recorded.")
            for e in r["events"]:
                st.markdown(f"`{e['node']}` ({e['kind']}) {_one_line(e)}")

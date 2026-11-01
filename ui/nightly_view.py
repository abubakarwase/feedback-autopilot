"""Animated view of the nightly run (app.nightly.run) for the right column of the demo UI.

Everything is drawn from the items nightly.run() yields ({"step", "message", "data"}); no number is made up here.
Reuses the fa-* CSS from ui/streamlit_app.py and adds a few nightly-only classes.
"""
import sqlite3
import time
from html import escape
from typing import Callable, Iterable

import streamlit as st

from app import config

STAGES = [
    ("Scan heatmaps", "reads the last 24 h of clicks in the app: angry repeated clicks, clicks that did nothing, and errors"),
    ("Find hotspots", "groups them by button or cell, ranks by users hit × clicks, skips what is already reported"),
    ("Agent handles hotspots", "each hotspot is treated like user feedback: the same agent fixes it or files an issue"),
    ("Check last fixes", "for every fix merged today: did the failed clicks and errors stop?"),
    ("Update trust", "re-scores past fixes developers reviewed; kinds with a clean record may now merge by themselves"),
    ("Refresh shared map", "rewrites each screen's explainer card with tonight's findings"),
]
NIGHTLY_PHASES = [("Look", [0, 1]), ("Act", [2, 3]), ("Learn", [4, 5])]
STAGE_OF = {"heatmap_scan": 1, "graph": 2, "graph_paused": 2, "impact_check": 3, "evals": 4, "map_builder": 5}
ONE_SHOT = {1, 4, 5}  # stages whose single item means the stage is finished
STAGE_PAUSE_S = 0.5
GRAPH_PAUSE_S = 0.3  # after fast (non-llm) graph steps, so the hotspot rows are watchable
VERDICT_ICON = {"confirmed": "✓", "worse": "✗", "no_change": "–", "skipped": "·"}

CSS = """<style>
.fa-chip { margin-left:6px; font-size:11px; font-weight:700; padding:1px 6px; border-radius:8px;
           background:rgba(255,255,255,.7); }
.fa-ndesc { font-size:12px; color:var(--fa-muted); margin:2px 0 6px; }
.fa-nrow { display:flex; align-items:baseline; gap:8px; font-size:13px; padding:5px 9px; margin:3px 0;
           border-radius:6px; background:var(--fa-surface); color:var(--fa-ink);
           transition: background-color 240ms cubic-bezier(0.22,1,0.36,1); }
.fa-nrow { flex-wrap:wrap; }
.fa-nrow b { white-space:nowrap; }
.fa-nrow .fa-nstate { margin-left:auto; white-space:normal; text-align:right; }
.fa-nrow .fa-ntext { color:var(--fa-muted); flex:1 1 100%; order:3; white-space:normal; font-size:12px; }
.fa-nrow.ok { background:var(--fa-done); color:var(--fa-done-ink); }
.fa-nrow.wait { background:var(--fa-wait); color:var(--fa-wait-ink); }
.fa-nrow.hit { background:var(--fa-done); color:var(--fa-done-ink); font-weight:600;
               outline:2px solid var(--fa-path); outline-offset:1px; }
.fa-nrow.bad { background:#f8d7d3; color:#7a1d12; }
.fa-dot { position:relative; display:inline-block; width:8px; height:8px; border-radius:50%;
          background:var(--fa-accent); margin-right:4px; }
.fa-dot::after { content:""; position:absolute; inset:-4px; border-radius:50%; border:2px solid var(--fa-accent);
                 animation: fa-ring 1.2s cubic-bezier(0.22,1,0.36,1) infinite; }
.fa-ntable { font-size:12px; border-collapse:collapse; margin:4px 0 2px; }
.fa-ntable td, .fa-ntable th { padding:2px 10px 2px 0; text-align:left; color:var(--fa-ink); }
.fa-ntable th { color:var(--fa-muted); font-weight:600; }
.fa-sum ul { margin:4px 0 0 18px; padding:0; }
@media (prefers-reduced-motion: reduce) {
  .fa-dot::after { animation:none; }
  .fa-nrow { transition:none; }
}
</style>"""


def trust_snapshot() -> dict[str, bool]:
    """'category · code area' -> trusted, read before the run so the view can show what changed."""
    con = sqlite3.connect(config.settings.DB_PATH)
    try:
        return {f"{c} · {a}": bool(t) for c, a, t in con.execute("SELECT category, code_area, trusted FROM trust")}
    finally:
        con.close()


def _parse(log: list[dict]) -> dict:
    p = {"sigs": [], "traces": {}, "paused": {}, "finished": set(), "checks": None, "evals": None, "cards": None,
         "last_stage": -1}
    prev_sid = None
    for it in log:
        step = it["step"]
        if step not in STAGE_OF:
            raise ValueError(f"unknown nightly step {step!r}")
        p["last_stage"] = max(p["last_stage"], STAGE_OF[step])
        if step == "heatmap_scan":
            p["sigs"] = it["data"]
        elif step == "graph":
            sid = it["signal_id"]
            if prev_sid is not None and prev_sid != sid:
                p["finished"].add(prev_sid)
            prev_sid = sid
            p["traces"].setdefault(sid, []).append(it["event"])
        elif step == "graph_paused":
            p["paused"][it["data"]["signal_id"]] = it["data"]["node"]
            p["finished"].add(it["data"]["signal_id"])
        elif step == "impact_check":
            p["checks"] = (p["checks"] or []) + ([it["data"]] if isinstance(it["data"], dict) else [])
        elif step == "evals":
            p["evals"] = it["data"]
        elif step == "map_builder":
            p["cards"] = it["data"]
    if p["last_stage"] > 2:
        p["finished"].update(s["id"] for s in p["sigs"])
    return p


def _trust_changes(p: dict, before: dict[str, bool]) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    combos = p["evals"]["scores"]["combos"]
    gained = [(n, c) for n, c in combos.items() if c["trusted"] and not before.get(n, False)]
    lost = [(n, c) for n, c in combos.items() if not c["trusted"] and before.get(n, False)]
    return gained, lost


def _chip(i: int, p: dict, before: dict[str, bool]) -> str:
    if i == 0:
        return "24 h read"
    if i == 1:
        n = len(p["sigs"])
        return f"{n} new hotspot{'s' * (n != 1)}" if n else "nothing new"
    if i == 2:
        n, waits = len(p["sigs"]), len(p["paused"])
        return (f"{n} handled" + (f", {waits} wait{'s' * (waits == 1)} for you" if waits else "")) if n else "nothing to do"
    if i == 3:
        checks = p["checks"] or []
        if not checks:
            return "nothing to check"
        conf = sum(c["verdict"] == "confirmed" for c in checks)
        worse = sum(c["verdict"] == "worse" for c in checks)
        return f"{conf} confirmed" + (f", {worse} worse" if worse else "")
    if i == 4:
        gained, lost = _trust_changes(p, before)
        if gained or lost:
            return ", ".join(x for x in (f"{len(gained)} newly trusted" if gained else "",
                                         f"{len(lost)} lost trust" if lost else "") if x)
        return f"{len(p['evals']['scores']['trusted'])} trusted, no change"
    return f"{len(p['cards'])} screens"


def _hotspot_rows(p: dict, running: bool, active: int | None, labels: dict, next_node: Callable,
                  outcome_of: Callable) -> str:
    current = None
    if running and active == 2:
        current = next((s["id"] for s in p["sigs"] if s["id"] not in p["finished"]), None)
    rows = []
    for s in p["sigs"]:
        sid, events = s["id"], p["traces"].get(s["id"], [])
        if sid in p["paused"]:
            cls, state = "wait", f"⏸ waits for a developer: {labels.get(p['paused'][sid], p['paused'][sid])}"
        elif sid == current:
            nxt = next_node(events[-1]) if events else "collect_signal"
            cls, state = "", f'<span class="fa-dot"></span>Now: {escape(labels.get(nxt, nxt) if nxt else "Finishing…")}'
        elif sid in p["finished"] and events:
            cls, state = "ok", escape(outcome_of(events))
        else:
            cls, state = "", "queued"
        rows.append(f'<div class="fa-nrow {cls}"><b>{escape(sid)}</b>'
                    f'<span class="fa-ntext" title="{escape(s["text"])}">{escape(s["text"])}</span>'
                    f'<span class="fa-nstate">{state}</span></div>')
    return "".join(rows)


def _details(p: dict, done_n: int, running: bool, active: int | None, before: dict[str, bool], labels: dict,
             next_node: Callable, outcome_of: Callable) -> str:
    out = []
    if done_n >= 2 and p["sigs"]:
        out.append('<div class="fa-lane">Tonight\'s hotspots, each handled by the agent (click one on the left for every step)</div>')
        out.append(_hotspot_rows(p, running, active, labels, next_node, outcome_of))
    elif done_n >= 2:
        out.append('<div class="fa-lane">Tonight\'s hotspots</div><div class="fa-nrow">No new hotspots in the last 24 h '
                   '(ones already reported are skipped).</div>')
    if p["checks"] is not None:
        out.append('<div class="fa-lane">Did the last fixes work? (before → after)</div>')
        if not p["checks"]:
            out.append('<div class="fa-nrow">No fixes were merged today, so nothing to check.</div>')
        for c in p["checks"]:
            cls = {"confirmed": "ok", "worse": "bad"}.get(c["verdict"], "")
            out.append(f'<div class="fa-nrow {cls}">{VERDICT_ICON[c["verdict"]]} '
                       f'{escape(c["message"].replace(" -> ", " → "))}</div>')
    if p["evals"] is not None:
        sc = p["evals"]["scores"]
        gained, lost = _trust_changes(p, before)
        out.append(f'<div class="fa-lane">Which kinds of fix may merge without a developer '
                   f'(based on {sc["cases"]} past reviewed fixes)</div>')
        for n, c in gained:
            out.append(f'<div class="fa-nrow hit">★ {escape(n)} is now trusted: {c["merged_unchanged"]}/{c["cases"]} '
                       f'merged unchanged, {c["reverts"]} reverts</div>')
        for n, c in lost:
            out.append(f'<div class="fa-nrow bad">✗ {escape(n)} lost trust: {c["reverts"]} revert(s) in '
                       f'{c["cases"]} cases</div>')
        shown = [(n, c) for n, c in sorted(sc["combos"].items(), key=lambda kv: (not kv[1]["trusted"], -kv[1]["cases"]))
                 if c["trusted"] or (n, c) in lost]
        body = "".join(f'<tr><td>{escape(n)}</td><td>{c["merged_unchanged"]}/{c["cases"]}</td><td>{c["reverts"]}</td>'
                       f'<td>{"yes" if c["trusted"] else "no"}</td></tr>' for n, c in shown)
        hidden = len(sc["combos"]) - len(shown)
        out.append('<table class="fa-ntable"><tr><th>kind of fix</th><th>kept unchanged</th><th>reverted</th>'
                   f'<th>trusted</th></tr>{body}</table>'
                   + (f'<div class="fa-ndesc">{hidden} other kinds not trusted yet (full table in Evals & trust)</div>'
                      if hidden else ""))
    if p["cards"] is not None:
        touched = {s["route"] for s in p["sigs"]} | {c["route"] for c in p["checks"] or []}
        out.append('<div class="fa-lane">Screen cards rewritten (green = changed by tonight\'s findings)</div>')
        out.append('<div class="fa-graph">' + "".join(
            f'<div class="fa-node {"done" if c["route"] in touched else ""}">{escape(c["screen"])} '
            f'<span style="opacity:.7">{escape(c["route"])}</span></div>' for c in p["cards"]) + "</div>")
    return "".join(out)


def nightly_html(log: list[dict], before: dict[str, bool], running: bool, labels: dict, next_node: Callable,
                 outcome_of: Callable, reveal: int | None = None) -> str:
    """Stage strip + status line + progress bar + per-stage results. reveal=k: show stage k as just started."""
    p = _parse(log)
    last = p["last_stage"]
    if reveal is not None:
        done_n, active = reveal, reveal
    elif not running:
        done_n, active = last + 1, None
    elif last == -1:
        done_n, active = 0, 0
    elif last in ONE_SHOT:
        done_n, active = last + 1, (last + 1 if last + 1 < len(STAGES) else None)
    else:
        done_n, active = last, last
    finished = done_n == len(STAGES)

    if active is not None:
        status = ("", f"Now: <b>{active + 1}. {STAGES[active][0]}</b> — {STAGES[active][1]}")
    elif finished:
        status = ("done", "Nightly run finished — summary below")
    else:
        status = ("", "Idle — press Run nightly on the left")
    frac = done_n / len(STAGES)
    if active == 2 and p["sigs"]:
        frac += len(p["finished"]) / len(p["sigs"]) / len(STAGES)

    def node(i):
        cls = "done" if i < done_n else "active" if i == active else ""
        chip = f'<span class="fa-chip">{escape(_chip(i, p, before))}</span>' if i < done_n else ""
        return f'<div class="fa-node {cls}">{i + 1}. {STAGES[i][0]}{chip}</div>'

    def arrow(i, glyph="→"):
        return f'<span class="fa-arrow {"taken" if i < done_n else "live" if i == active else ""}">{glyph}</span>'

    rows = "".join(
        f'<div class="fa-row"><div class="fa-phase">{ph}</div><div class="fa-graph">'
        + (arrow(idx[0], "↳") if idx[0] else "")
        + "".join((arrow(i) if i != idx[0] else "") + node(i) for i in idx) + "</div></div>"
        for ph, idx in NIGHTLY_PHASES)
    legend = ('<div class="fa-legend"><span style="background:var(--fa-accent)"></span>running now'
              '<span style="background:var(--fa-done)"></span>done'
              '<span style="background:var(--fa-wait)"></span>waiting for a human'
              '<span style="background:var(--fa-surface)"></span>not reached yet</div>')
    return (CSS + f'<div class="fa-status {status[0]}">{status[1]}</div>'
            f'<div class="fa-bar" title="{done_n} of {len(STAGES)} stages">'
            f'<div style="transform:scaleX({min(frac, 1.0):.3f})"></div></div>'
            + rows + legend
            + _details(p, done_n, running, active, before, labels, next_node, outcome_of))


def summary_html(log: list[dict], before: dict[str, bool], outcome_of: Callable) -> str:
    """3-5 plain-language results of a finished run."""
    p = _parse(log)
    ids = [s["id"] for s in p["sigs"]]
    bullets = [f"Found {len(ids)} new hotspot{'s' * (len(ids) != 1)}: {', '.join(ids)}" if ids
               else "No new hotspots in the last 24 h"]
    if ids:
        bullets.append("Agent: " + "; ".join(
            f"{sid} {'waits for a developer' if sid in p['paused'] else outcome_of(p['traces'][sid]).removeprefix('Done: ')}"
            for sid in ids if sid in p["traces"]))
    if p["checks"]:
        bullets.append("Impact check: " + "; ".join(c["message"].replace(" -> ", " → ") for c in p["checks"]))
    if p["evals"] is not None:
        gained, lost = _trust_changes(p, before)
        bits = [f"{n} is now trusted ({c['merged_unchanged']}/{c['cases']} merged unchanged, {c['reverts']} reverts)"
                for n, c in gained] + [f"{n} lost trust ({c['reverts']} reverts)" for n, c in lost]
        bullets.append("Trust: " + ("; ".join(bits) if bits else
                                    f"no change, trusted: {', '.join(p['evals']['scores']['trusted']) or 'none'}"))
    if p["cards"] is not None:
        bullets.append(f"Shared map refreshed: {len(p['cards'])} screen cards")
    return ('<div class="fa-status done fa-sum"><b>What the nightly run did</b><ul>'
            + "".join(f"<li>{escape(b)}</li>" for b in bullets) + "</ul></div>")


def run_live(items: Iterable[dict], box, log: list, traces: dict, before: dict[str, bool], labels: dict,
             next_node: Callable, outcome_of: Callable) -> None:
    """Consume nightly.run(), appending to log and traces in place; animate the stages in box (an st.empty)."""
    def show(reveal: int | None = None) -> None:
        box.markdown(nightly_html(log, before, True, labels, next_node, outcome_of, reveal), unsafe_allow_html=True)

    show()
    seen = -1
    for item in items:
        stage = STAGE_OF.get(item["step"])
        if stage is None:
            raise ValueError(f"unknown nightly step {item['step']!r}")
        if stage > seen:  # entering a new stage: show it running for a moment before its result lands
            show(reveal=stage)
            time.sleep(STAGE_PAUSE_S)
            seen = stage
        if item["step"] == "graph":
            traces.setdefault(item["signal_id"], []).append(item["event"])
        log.append(item)
        show()
        if item["step"] == "graph" and item["event"]["kind"] != "llm":
            time.sleep(GRAPH_PAUSE_S)


def render_finished(box, log: list[dict], before: dict[str, bool], labels: dict, next_node: Callable,
                    outcome_of: Callable) -> None:
    with box.container():
        st.markdown(nightly_html(log, before, False, labels, next_node, outcome_of), unsafe_allow_html=True)
        st.markdown(summary_html(log, before, outcome_of), unsafe_allow_html=True)
        with st.expander(f"Full nightly log ({len(log)} steps)"):
            for item in log:
                st.write(f"`{item['step']}` {item['message']}")

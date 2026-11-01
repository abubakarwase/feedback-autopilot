"""Feedback Autopilot demo UI.

Run from the repo root: uv run streamlit run ui/streamlit_app.py
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import config  # noqa: E402
from app.graph import AREAS, CATEGORIES, resume, run_signal  # noqa: E402
from app.tools import slack  # noqa: E402
from ui.map_view import render_map_tab  # noqa: E402

KIND_COLOR = {"tool": "blue", "llm": "violet", "python": "gray", "decision": "orange",
              "human": "green", "graph": "gray"}
KIND_LABEL = {"tool": "Tool", "llm": "AI", "python": "Rule", "decision": "Decision",
              "human": "Human", "graph": "Graph"}
SOURCE_COLOR = {"feedback": "blue", "heatmap": "orange"}
SOURCE_LABEL = {"feedback": "Feedback button", "heatmap": "Nightly heatmap"}

NODE_LABEL = {
    "collect_signal": "Read the feedback", "enrich": "Pull PostHog session",
    "extract_issue": "Understand the issue", "triage": "Triage",
    "locate_code": "Find the code", "reproduce": "Write a failing test", "fix": "Fix the code",
    "verify": "Run all tests", "open_pr": "Open a PR", "merge_gate": "Merge gate",
    "developer_review": "Developer review", "merge_and_notify": "Merge & tell the user",
    "human_triage": "Human triage", "link_duplicate": "Link duplicate",
    "open_issue": "Open an issue", "answer_how_to": "Answer from Screen Card",
}
MAIN_PATH = ["collect_signal", "enrich", "extract_issue", "triage", "locate_code", "reproduce", "fix",
             "verify", "open_pr", "merge_gate", "developer_review", "merge_and_notify"]
SIDE_EXITS = ["human_triage", "link_duplicate", "open_issue", "answer_how_to"]
STATIC_NEXT = {"collect_signal": "enrich", "enrich": "extract_issue", "extract_issue": "triage",
               "locate_code": "reproduce", "fix": "verify", "open_pr": "merge_gate"}
GATE_LABEL = {
    "test_red_to_green": "New test failed before the fix and passes after",
    "suite_and_lint": "Full test suite and lint pass",
    "diff_small": "Small change (≤ 40 lines in ≤ 3 files)",
    "no_protected_paths": "No protected files touched (billing, auth, CI …)",
    "confidence": "Agent is confident enough (≥ 85%)",
    "trusted": "This kind of fix has earned trust from past reviews",
    "not_spot_checked": "Not picked for the random spot check",
}
STEP_PAUSE_S = 0.4  # lets the audience follow the graph animation on fast (stubbed) steps

st.set_page_config(page_title="Feedback Autopilot", layout="wide")

st.markdown("""
<style>
:root { --fa-ink:#102a43; --fa-muted:#52606d; --fa-surface:#eef1f4; --fa-done:#d9efe2;
        --fa-done-ink:#14532d; --fa-accent:#1f4e79; --fa-wait:#f6e3b4; --fa-wait-ink:#6b4400; }
.fa-graph { display:flex; flex-wrap:wrap; align-items:center; gap:6px 4px; margin:4px 0 2px; }
.fa-node { position:relative; padding:7px 10px; border-radius:6px; background:var(--fa-surface);
           color:var(--fa-muted); font-size:13px; line-height:1.2;
           transition: background-color 240ms cubic-bezier(0.22,1,0.36,1), color 240ms; }
.fa-node.done { background:var(--fa-done); color:var(--fa-done-ink); }
.fa-node.active { background:var(--fa-accent); color:#fff; animation: fa-pulse 1.1s cubic-bezier(0.76,0,0.24,1) infinite; }
.fa-node.waiting { background:var(--fa-wait); color:var(--fa-wait-ink); font-weight:600;
                   animation: fa-pulse 1.1s cubic-bezier(0.76,0,0.24,1) infinite; }
.fa-node.skipped { opacity:.45; }
.fa-retry { margin-left:6px; font-size:11px; font-weight:700; }
.fa-arrow { color:#9aa5b1; font-size:13px; }
.fa-lane { font-size:12px; color:var(--fa-muted); margin:10px 0 2px; }
.fa-legend { font-size:12px; color:var(--fa-muted); margin-top:8px; }
.fa-legend span { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 10px;
                  vertical-align:-1px; }
@keyframes fa-pulse { 0%,100% { opacity:1; transform:scale(1); } 50% { opacity:.65; transform:scale(1.05); } }
@media (prefers-reduced-motion: reduce) { .fa-node.active, .fa-node.waiting { animation:none; } }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def _trace_store() -> dict:
    # Process-wide, so traces survive a browser refresh while the server runs.
    return {}


ss = st.session_state
if "traces" not in ss:
    ss.traces = _trace_store()  # signal_id -> list[TraceEvent dict]
ss.setdefault("selected", None)
ss.setdefault("pending", None)
ss.setdefault("nightly_log", [])


def _rows(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    path = config.settings.DB_PATH
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} not found; run: uv run python data/generate.py")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"{path}: {e}; run: uv run python data/generate.py") from e
    finally:
        con.close()


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def paused_at(sid: str) -> dict | None:
    """Interrupt payload if the last streamed event for sid is a pause, else None."""
    events = ss.traces.get(sid) or []
    if not events:
        return None
    last = events[-1]
    if last["kind"] == "human" and last["input"] is None and isinstance(last["output"], dict) \
            and "node" in last["output"]:
        return last["output"]
    return None


def status_of(sid: str) -> str:
    """Plain-language state of a signal, from its last trace event."""
    events = ss.traces.get(sid) or []
    if not events:
        return "Not run yet"
    pause = paused_at(sid)
    if pause:
        return ("Waiting for your review" if pause["node"] == "developer_review"
                else "Waiting for you to triage")
    last = events[-1]
    out = last.get("output") if isinstance(last.get("output"), dict) else {}
    return {
        "merge_and_notify": "Merged automatically" if out.get("by") == "auto" else "Merged after review",
        "open_issue": "Issue opened for a developer / product",
        "link_duplicate": "Linked as a duplicate",
        "answer_how_to": "Answered from the Screen Card",
        "developer_review": "Rejected by developer",
    }.get(last["node"], f"Stopped at: {NODE_LABEL.get(last['node'], last['node'])}")


# ---- agent graph (animated) ----------------------------------------------------

def _next_node(e: dict) -> str | None:
    out = e.get("output") if isinstance(e.get("output"), dict) else {}
    route = out.get("route")
    if route:
        return None if route == "end" else route
    return STATIC_NEXT.get(e["node"])


def graph_html(events: list[dict], running: bool) -> str:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["node"]] = counts.get(e["node"], 0) + 1
    state = {n: "done" for n in counts}
    pause = None
    if events:
        last = events[-1]
        if last["kind"] == "human" and last.get("input") is None and isinstance(last.get("output"), dict):
            pause = last["output"].get("node")
    if pause:
        state[pause] = "waiting"
    elif running:
        nxt = _next_node(events[-1]) if events else "collect_signal"
        if nxt:
            state[nxt] = "active"

    def node(n: str) -> str:
        retry = counts.get(n, 0) - 1
        badge = f'<span class="fa-retry">×{counts[n]}</span>' if retry > 0 else ""
        return f'<div class="fa-node {state.get(n, "")}">{NODE_LABEL[n]}{badge}</div>'

    arrow = '<span class="fa-arrow">→</span>'
    main = arrow.join(node(n) for n in MAIN_PATH)
    side = "".join(node(n) for n in SIDE_EXITS)
    legend = ('<div class="fa-legend">'
              '<span style="background:var(--fa-accent)"></span>running now'
              '<span style="background:var(--fa-done)"></span>done'
              '<span style="background:var(--fa-wait)"></span>waiting for a human'
              '<span style="background:var(--fa-surface)"></span>not reached · ×2 = ran twice (retry)</div>')
    return (f'<div class="fa-lane">Main path</div><div class="fa-graph">{main}</div>'
            f'<div class="fa-lane">Other ways a run can end</div><div class="fa-graph">{side}</div>{legend}')


# ---- trace rendering ---------------------------------------------------------

def _pct(p) -> str:
    return f"{p:.0%} sure" if isinstance(p, (int, float)) else ""


def step_summary(e: dict) -> str:
    """One plain-language line per trace event."""
    n, i = e["node"], e.get("input")
    o = e.get("output") if isinstance(e.get("output"), dict) else {}
    if n == "collect_signal":
        return f"“{o.get('text', '')}” — {o.get('user') or 'nightly scan'}, page {o.get('page_url', '')}"
    if n == "enrich":
        errs = len(o.get("console_errors") or [])
        return (f"{o.get('dead_clicks', 0)} dead clicks, {o.get('rage_clicks', 0)} rage clicks, {errs} console "
                f"errors · element {o.get('element_selector') or '–'}")
    if n == "extract_issue":
        return f"{o.get('element', '')} on {o.get('page', '')}: expected {o.get('expected', '')}, but {o.get('actual', '')}"
    if n == "triage":
        dup = f" · duplicate of {o['duplicate_of']}" if o.get("duplicate_of") else ""
        nxt = NODE_LABEL.get(o.get("route", ""), o.get("route", ""))
        low = " · confidence too low, collecting more evidence" if o.get("route") == "enrich" else ""
        return (f"{o.get('category', '')} · {o.get('code_area', '')} · agent can fix it: "
                f"{'yes' if o.get('fixable') else 'no'}{dup}{low} → next: {nxt}")
    if n == "human_triage":
        if i is None:
            return "Confidence stayed too low. Waiting for a developer to pick the category."
        return f"Developer chose {i.get('category')} · {i.get('code_area')}"
    if n == "developer_review":
        if i is None:
            pr = o.get("pr") or {}
            return f"PR #{pr.get('number', '?')} is waiting for a developer to approve or reject."
        return f"Developer chose: {i.get('action', i)}"
    if n == "merge_gate":
        route = o.get("route")
        return "All checks pass → merges by itself" if route == "merge_and_notify" else "Some checks fail → a developer reviews"
    if n == "merge_and_notify":
        return f"Merged ({'automatically' if o.get('by') == 'auto' else 'by the developer'}) and the forwarder was told in Slack"
    if n == "open_issue":
        return f"Issue #{o.get('number', '?')} opened with the evidence attached"
    if n == "link_duplicate":
        return "Linked to the existing report; evidence added there"
    if n == "open_pr":
        return f"PR #{o.get('number', '?')} opened on branch {o.get('branch', '')}"
    if n == "locate_code" and isinstance(e.get("output"), list):
        files = sorted({h.get("file", "") for h in e["output"] if isinstance(h, dict)})
        return f"{len(e['output'])} matches in {', '.join(files) or 'no files'}"
    if n in ("reproduce", "fix", "verify"):
        bits = []
        if o.get("test_path"):
            bits.append(f"test {o['test_path']}")
        if "test_failed_before" in o:
            bits.append("test failed before the fix ✓" if o["test_failed_before"] else "test did NOT fail ✗")
        if "tests_pass_after" in o:
            bits.append("all tests pass ✓" if o["tests_pass_after"] else "tests fail ✗")
        if o.get("files"):
            bits.append(f"{o.get('diff_lines', 0)} lines changed in {', '.join(o['files'])}")
        return " · ".join(bits) or NODE_LABEL.get(n, n)
    return NODE_LABEL.get(n, n)


def render_gate(gate: dict) -> None:
    checks = gate.get("checks") or {}
    auto = gate.get("route") == "merge_and_notify"
    st.markdown("**Merge gate → " + ("merges by itself, no developer needed**" if auto
                                      else "a developer must review**"))
    for k, v in checks.items():
        st.markdown(f"{'✅' if v else '❌'} {GATE_LABEL.get(k, k)}")


def render_event(e: dict) -> None:
    with st.container(border=True):
        c1, c2, c3 = st.columns([5, 1.2, 1.3])
        c1.markdown(f"**{NODE_LABEL.get(e['node'], e['node'])}**")
        with c2:
            st.badge(KIND_LABEL.get(e["kind"], e["kind"]), color=KIND_COLOR.get(e["kind"], "gray"))
        c3.caption(" · ".join(x for x in (_pct(e.get("p")), f"try {e['retry'] + 1}" if e.get("retry") else "",
                                          _when(e.get("ts"))) if x))
        st.markdown(step_summary(e))
        out = e.get("output")
        if e["node"] == "merge_gate" and isinstance(out, dict):
            render_gate(out)
        if e["kind"] == "human" and e.get("input") is None:
            st.warning("Paused — your decision is needed in the panel on the left.")
        with st.expander("details (raw data)"):
            if e.get("input") is not None:
                st.caption("input")
                st.json(e["input"], expanded=False)
            if out is not None:
                st.caption("output")
                st.json(out, expanded=False)


def stream_into(graph_box, box, sid: str, events) -> None:
    """Consume the whole generator; animate the graph and render each TraceEvent as it arrives."""
    trace = ss.traces.setdefault(sid, [])
    graph_box.markdown(graph_html(trace, running=True), unsafe_allow_html=True)
    with box:
        for ev in events:
            d = ev.model_dump()
            trace.append(d)
            graph_box.markdown(graph_html(trace, running=True), unsafe_allow_html=True)
            render_event(d)
            time.sleep(STEP_PAUSE_S)


# ---- left column -------------------------------------------------------------

def signal_row(sig: dict) -> None:
    sid = sig["id"]
    has_run = sid in ss.traces
    with st.container(border=True):
        top = st.columns([3.2, 1.6, 1.2])
        viewing = " · 👁 shown on the right" if ss.selected == sid else ""
        top[0].markdown(f"**{sid}** — {status_of(sid)}{viewing}")
        with top[1]:
            st.badge(SOURCE_LABEL.get(sig["source"], sig["source"]), color=SOURCE_COLOR.get(sig["source"], "gray"))
        if top[2].button("▶ Run agent" if not has_run else "↻ Run again", key=f"run-{sid}",
                         type="primary" if not has_run else "secondary", width="stretch"):
            ss.pending, ss.selected = ("run", sid, None), sid
            st.rerun()
        if st.button(f"“{sig['text']}”", key=f"show-{sid}", type="tertiary",
                     help="Show this signal's run on the right", disabled=not has_run):
            ss.selected = sid
            st.rerun()
        st.caption(f"{sig['user'] or 'nightly scan'} · {sig['route'] or ''} · {sig['page_url']}")
        if sig.get("file") and Path(sig["file"]).is_file():
            with st.expander("screenshot"):
                st.image(sig["file"])


def review_panel(sid: str, payload: dict) -> None:
    pr, fix = payload.get("pr") or {}, payload.get("fix") or {}
    with st.container(border=True):
        st.subheader(f"Your review is needed · {sid}")
        st.markdown(f"PR **#{pr.get('number')}** `{pr.get('branch')}` · state {pr.get('state')} · "
                    f"[{pr.get('url')}]({pr.get('url')})")
        st.caption(f"{fix.get('diff_lines')} diff lines in {', '.join(fix.get('files') or []) or 'no files'}"
                   f" · test {fix.get('test_path') or '-'}")
        render_gate(pr.get("gate_result") or {})
        a, r = st.columns(2)
        if a.button("Approve", key=f"approve-{sid}", type="primary", width="stretch"):
            ss.pending, ss.selected = ("resume", sid, {"action": "approve"}), sid
            st.rerun()
        if r.button("Reject", key=f"reject-{sid}", width="stretch"):
            ss.pending, ss.selected = ("resume", sid, {"action": "reject"}), sid
            st.rerun()


def triage_form(sid: str, payload: dict) -> None:
    t = payload.get("triage") or {}
    cats, areas = payload.get("categories") or CATEGORIES, payload.get("areas") or AREAS
    with st.form(f"triage-{sid}", border=True):
        st.subheader(f"Please triage · {sid}")
        st.caption(f"Agent guessed {t.get('category')} ({t.get('category_p')}) · "
                   f"{t.get('code_area')} ({t.get('code_area_p')}); the agent was not sure enough, even after collecting more evidence.")
        cat = st.selectbox("Category", cats, index=cats.index(t["category"]) if t.get("category") in cats else 0)
        area = st.selectbox("Code area", areas,
                            index=areas.index(t["code_area"]) if t.get("code_area") in areas else 0)
        if st.form_submit_button("Continue with this triage", type="primary"):
            ss.pending, ss.selected = ("resume", sid, {"category": cat, "code_area": area}), sid
            st.rerun()


def nightly_section() -> None:
    from app import nightly  # imported lazily: it needs heatmap/map_builder/evals

    if st.button("Run nightly", type="primary"):
        ss.nightly_log = []
        with st.status("Nightly run…", expanded=True) as status:
            for item in nightly.run():
                if item["step"] == "graph":
                    ss.traces.setdefault(item["signal_id"], []).append(item["event"])
                ss.nightly_log.append(item)
                st.write(f"`{item['step']}` {item['message']}")
            status.update(label="Nightly run done", state="complete", expanded=False)
        st.rerun()
    if ss.nightly_log:
        with st.expander(f"Last nightly run ({len(ss.nightly_log)} steps)"):
            for item in ss.nightly_log:
                st.write(f"`{item['step']}` {item['message']}")


def left_column() -> None:
    signals = slack.list_signals()
    for sig in signals:  # what waits for a human goes first
        pause = paused_at(sig["id"])
        if pause and pause["node"] == "developer_review":
            review_panel(sig["id"], pause)
        elif pause and pause["node"] == "human_triage":
            triage_form(sig["id"], pause)

    st.subheader("Incoming signals")
    if not signals:
        st.info("No signals yet. Send feedback from the target app (http://localhost:8000/app/) "
                "or run nightly.")
    for sig in signals:
        signal_row(sig)

    st.subheader("Nightly run (heatmap scan, impact check, evals, map)")
    nightly_section()

    st.subheader("Replies sent to forwarders (Slack)")
    replies = slack.list_replies()
    if not replies:
        st.caption("No replies yet.")
    for r in reversed(replies):
        st.markdown(f"**{r['signal_id']}** · {_when(r['ts'])}  \n{r['text']}")


# ---- right column ------------------------------------------------------------

def right_column() -> None:
    st.subheader("What the agent is doing" + (f" · {ss.selected}" if ss.selected else ""))
    graph_box = st.empty()
    st.markdown("**Step by step** (newest at the bottom)")
    if ss.pending:
        action, sid, decision = ss.pending
        ss.pending = None
        box = st.container()
        if action == "run":
            ss.traces[sid] = []
            with st.spinner(f"Agent is working on {sid}…"):
                stream_into(graph_box, box, sid, run_signal(sid))
        else:
            if paused_at(sid) is None:
                raise RuntimeError(f"{sid} is not paused; run_signal must be exhausted before resume")
            with st.spinner(f"Continuing {sid} with your decision…"):
                stream_into(graph_box, box, sid, resume(sid, decision))
        st.rerun()  # refresh the left column (review panel, replies)
    if not ss.selected or ss.selected not in ss.traces:
        graph_box.markdown(graph_html([], running=False), unsafe_allow_html=True)
        st.caption("Press ▶ Run agent on a signal to watch it move through the graph.")
        return
    graph_box.markdown(graph_html(ss.traces[ss.selected], running=False), unsafe_allow_html=True)
    for e in ss.traces[ss.selected]:
        render_event(e)


# ---- tabs ----------------------------------------------------------------------

def evals_tab() -> None:
    runs = _rows("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1")
    if runs:
        sc = json.loads(runs[0]["scores"])
        st.caption(f"Eval run #{runs[0]['id']} at {datetime.fromtimestamp(runs[0]['ts']):%Y-%m-%d %H:%M}")
        m = st.columns(4)
        m[0].metric("Triage accuracy", f"{sc.get('triage_accuracy', 0):.0%}")
        m[1].metric("Merged unchanged", f"{sc.get('merged_unchanged_rate', 0):.0%}")
        m[2].metric("Revert rate", f"{sc.get('revert_rate', 0):.1%}")
        m[3].metric("Cases", sc.get("cases", 0))
    else:
        st.info("No eval runs yet. Run nightly.")
    st.markdown("**Trust table**")
    trust = _rows("SELECT * FROM trust ORDER BY trusted DESC, cases DESC")
    st.dataframe([{"category · code area": f"{t['category']} · {t['code_area']}", "cases": t["cases"],
                   "merged unchanged": f"{t['merged_unchanged'] / t['cases']:.0%}" if t["cases"] else "-",
                   "reverts": t["reverts"], "trusted": bool(t["trusted"])} for t in trust],
                 hide_index=True, width="stretch")


# ---- page ----------------------------------------------------------------------

st.title("Feedback Autopilot")
st.caption("Forwarder feedback and PostHog hotspots in → triaged issue, failing test, fix and PR out. Proven kinds of fixes merge by themselves.")
tab_live, tab_map, tab_evals = st.tabs(["Live agent", "Shared map", "Evals & trust"])
with tab_live:
    left, right = st.columns([5, 6], gap="large")
    with left:
        left_column()
    with right:
        right_column()
with tab_map:
    render_map_tab()
with tab_evals:
    evals_tab()

"""Feedback Autopilot demo UI.

Run from the repo root: uv run streamlit run ui/streamlit_app.py
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import config  # noqa: E402
from app.graph import AREAS, CATEGORIES, resume, run_signal  # noqa: E402
from app.schemas import ScreenCard  # noqa: E402
from app.tools import slack  # noqa: E402

KIND_COLOR = {"tool": "blue", "llm": "violet", "python": "gray", "decision": "orange",
              "human": "green", "graph": "gray"}
SOURCE_COLOR = {"feedback": "blue", "heatmap": "orange"}
SCREENS = ["shipments", "invoice", "booking"]

st.set_page_config(page_title="Feedback Autopilot", layout="wide")


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
    events = ss.traces.get(sid) or []
    if not events:
        return ""
    pause = paused_at(sid)
    if pause:
        return f"waiting: {pause['node']}"
    return f"done: {events[-1]['node']}"


# ---- trace rendering ---------------------------------------------------------

def render_gate(gate: dict) -> None:
    checks = gate.get("checks") or {}
    route = gate.get("route", "")
    auto = route == "merge_and_notify"
    st.markdown(f"**Merge gate:** {':green-background[AUTO-MERGE]' if auto else ':orange-background[DEVELOPER REVIEW]'}")
    st.table([{"check": k, "result": "pass" if v else "FAIL"} for k, v in checks.items()])


def render_event(e: dict) -> None:
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([3, 1.3, 1, 1, 1.2])
        c1.markdown(f"**{e['node']}**")
        with c2:
            st.badge(e["kind"], color=KIND_COLOR.get(e["kind"], "gray"))
        c3.markdown(f"p **{e['p']:.2f}**" if e.get("p") is not None else "p –")
        c4.markdown(f"retry {e.get('retry', 0)}")
        c5.caption(_when(e.get("ts")))
        out = e.get("output")
        if e["node"] == "merge_gate" and isinstance(out, dict):
            render_gate(out)
        if e["kind"] == "human" and e.get("input") is None:
            st.warning(f"Paused: waiting for a human at {e['node']}")
        if isinstance(out, dict) and out.get("route"):
            st.caption(f"route -> {out['route']}")
        if e.get("input") is not None:
            with st.expander("input"):
                st.json(e["input"], expanded=False)
        if out is not None:
            with st.expander("output"):
                st.json(out, expanded=False)


def stream_into(box, sid: str, events) -> None:
    """Consume the whole generator, rendering and storing each TraceEvent as it arrives."""
    trace = ss.traces.setdefault(sid, [])
    with box:
        for ev in events:
            d = ev.model_dump()
            trace.append(d)
            render_event(d)


# ---- left column -------------------------------------------------------------

def signal_row(sig: dict) -> None:
    sid = sig["id"]
    with st.container(border=True):
        top = st.columns([1.2, 1.2, 3, 1, 1])
        top[0].markdown(f"**{sid}**")
        with top[1]:
            st.badge(sig["source"], color=SOURCE_COLOR.get(sig["source"], "gray"))
        top[2].caption(status_of(sid))
        if top[3].button("Run" if sid not in ss.traces else "Re-run", key=f"run-{sid}",
                         type="primary" if sid not in ss.traces else "secondary"):
            ss.pending, ss.selected = ("run", sid, None), sid
            st.rerun()
        if top[4].button("Trace", key=f"show-{sid}", disabled=sid not in ss.traces):
            ss.selected = sid
            st.rerun()
        st.markdown(sig["text"])
        st.caption(f"{sig['user'] or 'nightly'} · {sig['route'] or ''} · {sig['page_url']}")
        if sig.get("file") and Path(sig["file"]).is_file():
            with st.expander("screenshot"):
                st.image(sig["file"])


def review_panel(sid: str, payload: dict) -> None:
    pr, fix = payload.get("pr") or {}, payload.get("fix") or {}
    with st.container(border=True):
        st.subheader(f"Developer review · {sid}")
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
        st.subheader(f"Human triage · {sid}")
        st.caption(f"Agent guessed {t.get('category')} ({t.get('category_p')}) · "
                   f"{t.get('code_area')} ({t.get('code_area_p')}); confidence too low after retries.")
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

    st.subheader("Signals")
    if not signals:
        st.info("No signals yet. Send feedback from the target app (http://localhost:8000/app/) "
                "or run nightly.")
    for sig in signals:
        signal_row(sig)

    st.subheader("Nightly")
    nightly_section()

    st.subheader("Slack replies")
    replies = slack.list_replies()
    if not replies:
        st.caption("No replies yet.")
    for r in reversed(replies):
        st.markdown(f"**{r['signal_id']}** · {_when(r['ts'])}  \n{r['text']}")


# ---- right column ------------------------------------------------------------

def right_column() -> None:
    st.subheader("Live trace" + (f" · {ss.selected}" if ss.selected else ""))
    if ss.pending:
        action, sid, decision = ss.pending
        ss.pending = None
        box = st.container()
        if action == "run":
            ss.traces[sid] = []
            with st.spinner(f"Running {sid}…"):
                stream_into(box, sid, run_signal(sid))
        else:
            if paused_at(sid) is None:
                raise RuntimeError(f"{sid} is not paused; run_signal must be exhausted before resume")
            with st.spinner(f"Resuming {sid}…"):
                stream_into(box, sid, resume(sid, decision))
        st.rerun()  # refresh the left column (review panel, replies)
    if not ss.selected or ss.selected not in ss.traces:
        st.caption("Press Run on a signal to watch the graph step by step.")
        return
    for e in ss.traces[ss.selected]:
        render_event(e)


# ---- tabs ----------------------------------------------------------------------

def render_view(view, label: str) -> None:
    st.markdown(f"#### {label} view")
    st.markdown(f"**What happens here**  \n{view.what_happens}")
    st.markdown(f"**How people use it**  \n{view.how_people_use_it}")
    if view.words:
        st.markdown("**Words**")
        st.table([{"forwarder says": w.forwarder_term, "code calls it": w.code_name} for w in view.words])
    st.markdown(f"**Where it breaks**  \n{view.where_it_breaks}")
    st.markdown(f"**What changed**  \n{view.what_changed}")


def map_tab() -> None:
    c1, c2, c3 = st.columns([2, 2, 1])
    screen = c1.segmented_control("Screen", SCREENS, default=SCREENS[0])
    who = c2.segmented_control("View", ["Forwarder", "Developer"], default="Forwarder")
    if c3.button("Rebuild map"):
        from app import map_builder

        with st.spinner("Building screen cards…"):
            map_builder.build_all()
        st.rerun()
    if not screen or not who:
        return
    rows = _rows("SELECT * FROM screen_cards WHERE screen = ?", (screen,))
    if not rows:
        st.info(f"No card for {screen} yet. Run nightly or Rebuild map.")
        return
    row = rows[0]
    card = ScreenCard.model_validate_json(row["card"])
    st.caption(f"{card.screen} · route {card.route} · version {row['version']} · updated {row['updated_at']}")
    render_view(card.forwarder if who == "Forwarder" else card.developer, who)
    st.markdown("**Sources**")
    st.table([{"kind": s.kind, "ref": s.ref} for s in card.sources])


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
tab_live, tab_map, tab_evals = st.tabs(["Live", "Shared map", "Evals"])
with tab_live:
    left, right = st.columns([5, 6], gap="large")
    with left:
        left_column()
    with right:
        right_column()
with tab_map:
    map_tab()
with tab_evals:
    evals_tab()

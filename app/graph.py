"""LangGraph pipeline. Edges are fixed here; LLM runs only inside nodes."""
import hashlib
import operator
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Iterator, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from app import config
from app.decide import decide
from app.llm import llm
from app.mcp_client import labels_call
from app.schemas import Evidence, FixAttempt, Issue, PullRequest, Signal, Triage, TraceEvent
from app.tools import github, posthog, repo, slack

# Question strings decide() is keyed on (data/decide_fixtures.json must use these).
Q_CATEGORY = "Which category is this feedback?"
Q_AREA = "Which code area does this belong to?"
Q_FIXABLE = "Can an agent fix this with a small code change?"
CATEGORIES = ["bug", "copy", "ux", "feature_request", "how_to"]
AREAS = ["handler-frontend", "frontend", "backend"]


class State(TypedDict, total=False):
    signal_id: str
    signal: dict
    evidence: dict
    issue: dict
    triage: dict
    fix: dict
    pr: dict
    retries: dict
    trace: Annotated[list, operator.add]
    decision: dict
    route: str


class ReproTest(BaseModel):
    path: str
    code: str


def _ev(node: str, kind: str, input: Any = None, output: Any = None,
        p: float | None = None, retry: int = 0) -> dict:
    return TraceEvent(node=node, kind=kind, input=input, output=output, p=p,
                      retry=retry, ts=time.time()).model_dump()


def _retries(s: State, key: str) -> int:
    return (s.get("retries") or {}).get(key, 0)


def _bump(s: State, key: str) -> dict:
    return {**(s.get("retries") or {}), key: _retries(s, key) + 1}


# ---- nodes ----------------------------------------------------------------

def collect_signal(s: State) -> dict:
    fb = slack.get_feedback(s["signal_id"])
    sig = Signal(id=s["signal_id"], text=fb["text"], user=fb["user"], channel=fb["channel"],
                 screenshot=fb.get("file"), page_url=fb["page_url"], ts=fb["ts"]).model_dump()
    return {"signal": sig, "trace": [_ev("collect_signal", "tool", s["signal_id"], sig)]}


def enrich(s: State) -> dict:
    url = s["signal"]["page_url"]
    events = posthog.query_events("$dead_click", url)
    heat = posthog.get_heatmap(url)
    ev = Evidence(session_events=events, dead_clicks=len(events),
                  element_selector=(events[0]["$elements_chain"] if events else ""),
                  heatmap_stats=heat).model_dump()
    return {"evidence": ev, "trace": [_ev("enrich", "tool", url, ev, retry=_retries(s, "triage"))]}


def extract_issue(s: State) -> dict:
    shot = s["signal"].get("screenshot")
    images = [Path(shot).read_bytes()] if shot and Path(shot).is_file() else None
    issue = llm("extract_issue", Issue,
                f"Feedback: {s['signal']['text']}\nEvidence: {s['evidence']}", images)
    out = issue.model_dump()
    return {"issue": out, "trace": [_ev("extract_issue", "llm", s["signal"]["text"], out)]}


def triage(s: State) -> dict:
    sid, n = s["signal_id"], _retries(s, "triage") + 1
    ctx = {"issue": s["issue"], "evidence": s["evidence"]}
    cat = decide(Q_CATEGORY, CATEGORIES, ctx, sid, n)
    area = decide(Q_AREA, AREAS, ctx, sid, n)
    fix = decide(Q_FIXABLE, ["yes", "no"], ctx, sid, n)
    dups = labels_call("find_duplicates", text=s["signal"]["text"], page_url=s["signal"]["page_url"])
    t = Triage(duplicate_of=(dups[0].get("signal_id") or dups[0].get("id")) if dups else None,
               category=cat["answer"], category_p=cat["p"], code_area=area["answer"],
               code_area_p=area["p"], fixable=fix["answer"] == "yes", fixable_p=fix["p"])
    low = t.p < config.settings.CONFIDENCE_THRESHOLD and not t.duplicate_of
    update: dict = {}
    if low and _retries(s, "triage") < config.settings.MAX_RETRIES:
        update = {"route": "enrich", "retries": _bump(s, "triage")}
        out = {**t.model_dump(), "p": t.p, "route": "enrich"}
        return {**update, "triage": t.model_dump(),
                "trace": [_ev("triage", "decision", ctx, out, t.p, n - 1)]}
    pre = []
    if low:  # retries exhausted: a human decides
        human = interrupt({"node": "human_triage", "signal_id": sid, "triage": t.model_dump(),
                           "categories": CATEGORIES, "areas": AREAS})
        t = t.model_copy(update={"category": human["category"], "code_area": human["code_area"],
                                 "category_p": 1.0, "code_area_p": 1.0})
        labels_call("save_label", signal_id=sid, category=t.category, code_area=t.code_area,
                    outcome="triaged_by_human")
        pre = [_ev("human_triage", "human", human, t.model_dump(), 1.0, n - 1)]
    route = ("link_duplicate" if t.duplicate_of else "open_issue" if not t.fixable else "locate_code")
    return {"triage": t.model_dump(), "route": route,
            "trace": pre + [_ev("triage", "decision", ctx, {**t.model_dump(), "route": route}, t.p, n - 1)]}


def link_duplicate(s: State) -> dict:
    out = slack.post_reply(s["signal_id"], f"Duplicate of {s['triage']['duplicate_of']}")
    return {"trace": [_ev("link_duplicate", "tool", s["triage"]["duplicate_of"], out)]}


def open_issue(s: State) -> dict:
    out = github.create_issue(f"[{s['signal_id']}] {s['issue']['page']}", str(s.get("fix") or s["issue"]))
    return {"trace": [_ev("open_issue", "tool", s["signal_id"], out)]}


def locate_code(s: State) -> dict:
    hits = repo.search_code(s["evidence"].get("element_selector") or s["issue"]["element"])
    issue = {**s["issue"], "candidate_files": sorted({h["file"] for h in hits})}
    return {"issue": issue, "trace": [_ev("locate_code", "tool", s["issue"]["element"], hits)]}


def reproduce(s: State) -> dict:
    n = _retries(s, "reproduce")
    t = llm("reproduce", ReproTest, f"Write a failing test for: {s['issue']}")
    wt = repo.create_worktree(f"fix/{s['signal_id']}")
    res = repo.run_tests(wt, t.path)
    failed = not res["passed"]
    update: dict = {"fix": {"branch": f"fix/{s['signal_id']}", "test_path": t.path,
                            "test_failed_before": failed}}
    route = "fix"
    if not failed:
        route = "enrich" if n < config.settings.MAX_RETRIES else "open_issue"
        if route == "enrich":
            update["retries"] = _bump(s, "reproduce")
    return {**update, "route": route,
            "trace": [_ev("reproduce", "llm", s["issue"], {"test": t.model_dump(), "failed": failed,
                                                            "route": route}, retry=n)]}


def fix(s: State) -> dict:
    n = _retries(s, "verify")
    st = repo.diff_stats(repo.create_worktree(s["fix"]["branch"]))  # STUB: coder.code_fix() later
    f = {**s["fix"], "diff_lines": st["lines"], "files": st["files"],
         "touched_protected_paths": st["protected"], "p": 0.9}
    return {"fix": f, "trace": [_ev("fix", "llm", s["issue"], f, f["p"], n)]}


def verify(s: State) -> dict:
    n = _retries(s, "verify")
    res = repo.run_tests(repo.create_worktree(s["fix"]["branch"]))
    ok = res["passed"]
    f = FixAttempt(**{**s["fix"], "tests_pass_after": ok, "lint_ok": res["lint_ok"]}).model_dump()
    route = "open_pr" if ok else ("fix" if n < config.settings.MAX_RETRIES else "open_issue")
    update = {"retries": _bump(s, "verify")} if route == "fix" else {}
    return {**update, "fix": f, "route": route, "trace": [_ev("verify", "tool", s["fix"]["branch"], res, retry=n)]}


def open_pr(s: State) -> dict:
    pr = github.open_pr(s["fix"]["branch"], f"Fix {s['signal_id']}: {s['issue']['page']}", str(s["issue"]))
    return {"pr": PullRequest(**pr).model_dump(), "trace": [_ev("open_pr", "tool", s["fix"]["branch"], pr)]}


def _spot_checked(signal_id: str) -> bool:
    h = int(hashlib.sha1(signal_id.encode()).hexdigest(), 16) % 1000 / 1000
    return h < config.settings.SPOT_CHECK_RATE


def merge_gate(s: State) -> dict:
    cfg, f, t = config.settings, s["fix"], s["triage"]
    trust = labels_call("get_trust", category=t["category"], code_area=t["code_area"])
    p = min(t["category_p"], t["code_area_p"], t["fixable_p"], f["p"])
    checks = {
        "test_red_to_green": f["test_failed_before"] and f["tests_pass_after"],
        "suite_and_lint": f["tests_pass_after"] and f["lint_ok"],
        "diff_small": f["diff_lines"] <= cfg.MAX_DIFF_LINES and len(f["files"]) <= cfg.MAX_DIFF_FILES,
        "no_protected_paths": not f["touched_protected_paths"],
        "confidence": p >= cfg.CONFIDENCE_THRESHOLD,
        "trusted": bool(trust.get("trusted")),
        "not_spot_checked": not _spot_checked(s["signal_id"]),
    }
    route = "merge_and_notify" if all(checks.values()) else "developer_review"
    pr = {**s["pr"], "gate_result": {"checks": checks, "route": route}}
    return {"pr": pr, "route": route, "trace": [_ev("merge_gate", "python", f, pr["gate_result"], p)]}


def developer_review(s: State) -> dict:
    github.request_review(s["pr"]["number"])
    d = interrupt({"node": "developer_review", "signal_id": s["signal_id"], "pr": s["pr"],
                   "fix": s["fix"], "options": ["approve", "reject"]})
    t = s["triage"]
    approved = d.get("action") == "approve"
    labels_call("save_label", signal_id=s["signal_id"], category=t["category"], code_area=t["code_area"],
                outcome=(d.get("outcome") or ("merged_unchanged" if approved else "rejected")))
    pr = {**s["pr"], "state": "needs_review" if approved else "rejected"}
    return {"decision": d, "pr": pr, "route": "merge_and_notify" if approved else "end",
            "trace": [_ev("developer_review", "human", s["pr"], d)]}


def merge_and_notify(s: State) -> dict:
    by = "developer" if s.get("decision") else "auto"
    m = github.merge_pr(s["pr"]["number"])
    reply = slack.post_reply(s["signal_id"], "Fixed. Please try again.")
    pr = {**s["pr"], "state": "merged", "merged_by": by}
    return {"pr": pr, "trace": [_ev("merge_and_notify", "tool", s["pr"]["number"], {"merge": m, "reply": reply, "by": by})]}


# ---- wiring ---------------------------------------------------------------

def _route(s: State) -> str:
    return s["route"]


def build(checkpointer) -> Any:
    g = StateGraph(State)
    for fn in (collect_signal, enrich, extract_issue, triage, locate_code, reproduce, fix, verify,
               open_pr, merge_gate, developer_review, merge_and_notify, link_duplicate, open_issue):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "collect_signal")
    g.add_edge("collect_signal", "enrich")
    g.add_edge("enrich", "extract_issue")
    g.add_edge("extract_issue", "triage")
    g.add_conditional_edges("triage", _route, ["enrich", "link_duplicate", "open_issue", "locate_code"])
    g.add_edge("locate_code", "reproduce")
    g.add_conditional_edges("reproduce", _route, ["fix", "enrich", "open_issue"])
    g.add_edge("fix", "verify")
    g.add_conditional_edges("verify", _route, ["open_pr", "fix", "open_issue"])
    g.add_edge("open_pr", "merge_gate")
    g.add_conditional_edges("merge_gate", _route, ["merge_and_notify", "developer_review"])
    g.add_conditional_edges("developer_review", _route,
                            {"merge_and_notify": "merge_and_notify", "end": END})
    for end in ("merge_and_notify", "link_duplicate", "open_issue"):
        g.add_edge(end, END)
    return g.compile(checkpointer=checkpointer)


@lru_cache
def _graph(path: str):
    return build(SqliteSaver(sqlite3.connect(path, check_same_thread=False)))


def _stream(inp: Any, signal_id: str) -> Iterator[TraceEvent]:
    cfg = {"configurable": {"thread_id": signal_id}}
    for chunk in _graph(config.settings.CHECKPOINT_PATH).stream(inp, cfg, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__":
                for i in update:
                    yield TraceEvent(node=i.value["node"], kind="human", output=i.value, ts=time.time())
                continue
            for e in (update or {}).get("trace", []):
                yield TraceEvent(**e)


def run_signal(signal_id: str) -> Iterator[TraceEvent]:
    return _stream({"signal_id": signal_id, "retries": {}, "trace": []}, signal_id)


def resume(signal_id: str, decision: dict) -> Iterator[TraceEvent]:
    return _stream(Command(resume=decision), signal_id)


if __name__ == "__main__":
    import sys

    sid = sys.argv[1] if len(sys.argv) > 1 else "FB-101"
    events = list(run_signal(sid))
    if events and events[-1].kind == "human":
        events += list(resume(sid, {"action": "approve", "category": "bug", "code_area": "handler-frontend"}))
    for e in events:
        print(e.model_dump_json())

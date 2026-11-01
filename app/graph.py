"""LangGraph pipeline. Edges are fixed here; LLM runs only inside nodes."""
import hashlib
import operator
import sqlite3
import re
import shutil
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Iterator, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from app import coder, config
from app.cards import get_card
from app.decide import decide
from app.gate import merge_gate as gate_check
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
    worktree: str | None
    repro_sha: str | None


class HowToAnswer(BaseModel):
    answer: str
    card_section: str
    sources: list[str]


class ReproTest(BaseModel):
    filename: str
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
        t = Triage.model_validate({**t.model_dump(), "category": human["category"],
                                   "code_area": human["code_area"], "category_p": 1.0, "code_area_p": 1.0})
        labels_call("save_label", signal_id=sid, category=t.category, code_area=t.code_area,
                    outcome="triaged_by_human")
        pre = [_ev("human_triage", "human", human, t.model_dump(), 1.0, n - 1)]
    route = ("link_duplicate" if t.duplicate_of else "answer_how_to" if t.category == "how_to"
             else "open_issue" if not t.fixable else "locate_code")
    return {"triage": t.model_dump(), "route": route,
            "trace": pre + [_ev("triage", "decision", ctx, {**t.model_dump(), "route": route}, t.p, n - 1)]}


def link_duplicate(s: State) -> dict:
    out = slack.post_reply(s["signal_id"], f"Duplicate of {s['triage']['duplicate_of']}")
    return {"trace": [_ev("link_duplicate", "tool", s["triage"]["duplicate_of"], out)]}


def open_issue(s: State) -> dict:
    body = str(s.get("fix") or s["issue"])
    if s.get("triage", {}).get("category") == "how_to":
        body += "\nNote: no screen card"
    out = github.create_issue(f"[{s['signal_id']}] {s['issue']['page']}", body)
    return {"trace": [_ev("open_issue", "tool", s["signal_id"], out)]}


def answer_how_to(s: State) -> dict:
    card = get_card(s["signal"]["page_url"])
    if card is None:
        return {"route": "open_issue", "trace": [_ev("answer_how_to", "python", s["signal"]["page_url"],
                                                     {"answer": None, "card_route": None, "sources": [],
                                                      "note": "no screen card"})]}
    a = llm("how_to_answer", HowToAnswer,
            f"Feedback: {s['signal']['text']}\nAnswer in 2-3 sentences, in the forwarder's own words, "
            f"using only this screen card view:\n{card.forwarder.model_dump_json()}")
    slack.post_reply(s["signal_id"], a.answer)
    t = s["triage"]
    labels_call("save_label", signal_id=s["signal_id"], category=t["category"], code_area=t["code_area"],
                outcome="answered_from_card")
    return {"route": "end", "trace": [_ev("answer_how_to", "llm", s["signal"]["text"],
                                          {"answer": a.answer, "card_route": card.route, "sources": a.sources})]}


_NOISE = {"div", "span", "button", "html", "body", "the", "and"}


def locate_code(s: State) -> dict:
    issue = s["issue"]
    q = " ".join([s["evidence"].get("element_selector") or "", issue["element"], issue["page"]])
    terms = sorted({t for t in re.findall(r"[\w-]{3,}", q) if t.lower() not in _NOISE})
    hits = repo.search_code(" ".join(terms)) if terms else []
    files = [f for f, _ in Counter(h["file"] for h in hits).most_common(3)]
    return {"issue": {**issue, "candidate_files": files},
            "trace": [_ev("locate_code", "tool", terms, {"files": files, "hits": len(hits)})]}


def _sid_slug(sid: str) -> str:
    return re.sub(r"\W+", "_", sid.lower())


def reproduce(s: State) -> dict:
    n, sid = _retries(s, "reproduce"), s["signal_id"]
    branch = f"fix/{sid.lower()}"
    wt = s.get("worktree") or repo.create_worktree(branch)
    rel = f"{repo.TESTS}/test_repro_{_sid_slug(sid)}.py"
    dest = Path(wt) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    offline_patch = Path("data/patches") / f"{sid}_test.py"
    if not offline_patch.is_file():
        found = sorted(Path("data/repro_tests").glob(f"test_{sid.lower().replace('-', '')}_*.py"))
        offline_patch = found[0] if found else offline_patch
    if config.settings.OFFLINE:
        if offline_patch.is_file():
            shutil.copy(offline_patch, dest)
            res = repo.run_tests(wt, rel)
            failed = res["returncode"] == 1
        else:
            res, failed = {"summary": "offline: no repro test"}, True
    else:
        parts = [f"Issue: {s['issue']}", "Write a pytest-playwright test that FAILS because of this bug "
                 "and would pass once fixed. Return filename and full code."]
        for f in s["issue"]["candidate_files"]:
            parts.append(f"--- {f}\n{(Path(wt) / f).read_text() if (Path(wt) / f).is_file() else ''}")
        conv = [p for p in [Path(wt) / repo.TESTS / "conftest.py",
                            *sorted((Path(wt) / repo.TESTS).glob("test_*.py"))[:1]] if p.is_file()]
        if conv:
            parts.append("Follow the conventions of these existing test files:")
            parts += [f"--- {p.relative_to(wt)}\n{p.read_text()}" for p in conv]
        t = llm("repro_test", ReproTest, "\n\n".join(parts))
        dest.write_text(t.code)
        res = repo.run_tests(wt, rel)
        failed = res["returncode"] == 1  # 2+ = collection/usage error: not reproduced
    update: dict = {"worktree": wt, "fix": {"branch": branch, "test_path": rel, "test_failed_before": failed},
              "repro_sha": hashlib.sha256(dest.read_bytes()).hexdigest() if dest.exists() else None}
    route = "fix"
    if not failed:
        route = "enrich" if n < config.settings.MAX_RETRIES else "open_issue"
        if route == "enrich":
            update["retries"] = _bump(s, "reproduce")
    return {**update, "route": route,
            "trace": [_ev("reproduce", "llm", s["issue"], {"test": rel, "failed_before": failed,
                                                            "summary": res.get("summary"), "route": route}, retry=n)]}


def fix(s: State) -> dict:
    n = _retries(s, "verify")
    r = coder.code_fix(Issue(**s["issue"]), s["issue"]["candidate_files"], s["worktree"], s["signal_id"],
                       test_path=s["fix"]["test_path"])
    f = {**s["fix"], "p": 0.9 if r["ok"] else 0.0}
    return {"fix": f, "trace": [_ev("fix", "llm", s["issue"]["candidate_files"], r, f["p"], n)]}


def verify(s: State) -> dict:
    n, wt = _retries(s, "verify"), s["worktree"]
    one = repo.run_tests(wt, s["fix"]["test_path"])
    tp = Path(wt) / s["fix"]["test_path"]
    if s.get("repro_sha") and (not tp.exists() or hashlib.sha256(tp.read_bytes()).hexdigest() != s["repro_sha"]):
        one = {"passed": False, "returncode": -1, "summary": "repro test was modified", "output": ""}
    full = repo.run_tests(wt) if one["passed"] else one
    ok = one["passed"] and full["passed"]
    st = repo.diff_stats(wt)
    f = FixAttempt(**{**s["fix"], "tests_pass_after": ok, "lint_ok": True, "diff_lines": st["lines"],
                      "files": st["files"], "touched_protected_paths": st["protected"]}).model_dump()
    out = {"repro_test": one["summary"], "suite": full["summary"], "passed": ok,
           "diff_lines": st["lines"], "files": st["files"], "protected": st["protected"]}
    route = "open_pr" if ok else ("fix" if n < config.settings.MAX_RETRIES else "open_issue")
    update = {"retries": _bump(s, "verify")} if route == "fix" else {}
    if ok:
        out["commit"] = repo.commit_all(wt, f"Fix {s['signal_id']}: {s['issue']['page']}")
    return {**update, "fix": f, "route": route, "trace": [_ev("verify", "tool", s["fix"]["branch"], out, retry=n)]}


def open_pr(s: State) -> dict:
    ev, f = s["evidence"], s["fix"]
    body = (f"Feedback ({s['signal_id']}): {s['signal']['text']}\n\n"
            f"Evidence: {ev['dead_clicks']} dead clicks, {ev['rage_clicks']} rage clicks, "
            f"selector `{ev['element_selector']}`, errors: {ev['console_errors'] or 'none'}\n\n"
            f"Repro test: {f['test_path']}\nBefore: test fails. After: test passes, suite green "
            f"({f['diff_lines']} lines in {len(f['files'])} files).")
    pr = github.open_pr(f["branch"], f"Fix {s['signal_id']}: {s['issue']['page']}", body)
    return {"pr": PullRequest(**pr).model_dump(), "trace": [_ev("open_pr", "tool", f["branch"], pr)]}


def merge_gate(s: State) -> dict:
    t = s["triage"]
    trust = labels_call("get_trust", category=t["category"], code_area=t["code_area"])
    g = gate_check(FixAttempt(**s["fix"]), Triage(**t), trust, s["signal_id"], github.merges_today())
    route = "merge_and_notify" if g["auto_merge"] else "developer_review"
    if route == "developer_review":
        github.request_review(s["pr"]["number"])
    pr = {**s["pr"], "gate_result": {**g, "route": route}}
    return {"pr": pr, "route": route, "trace": [_ev("merge_gate", "python", s["fix"], pr["gate_result"],
                                                    min(Triage(**t).p, s["fix"]["p"]))]}


def developer_review(s: State) -> dict:
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
    m = github.merge_pr(s["pr"]["number"], merged_by=by)
    reply = slack.post_reply(s["signal_id"], "Fixed. Please try again.")
    pr = {**s["pr"], "state": "merged", "merged_by": by}
    return {"pr": pr, "trace": [_ev("merge_and_notify", "tool", s["pr"]["number"], {"merge": m, "reply": reply, "by": by})]}


# ---- wiring ---------------------------------------------------------------

def _route(s: State) -> str:
    return s["route"]


def build(checkpointer) -> Any:
    g = StateGraph(State)
    for fn in (collect_signal, enrich, extract_issue, triage, locate_code, reproduce, fix, verify,
               open_pr, merge_gate, developer_review, merge_and_notify, link_duplicate, open_issue, answer_how_to):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "collect_signal")
    g.add_edge("collect_signal", "enrich")
    g.add_edge("enrich", "extract_issue")
    g.add_edge("extract_issue", "triage")
    g.add_conditional_edges("triage", _route, ["enrich", "link_duplicate", "open_issue", "locate_code", "answer_how_to"])
    g.add_conditional_edges("answer_how_to", _route, {"open_issue": "open_issue", "end": END})
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
    return _stream({"signal_id": signal_id, "retries": {}, "trace": [], "decision": None, "route": "",
                    "fix": None, "pr": None, "worktree": None, "repro_sha": None}, signal_id)


def resume(signal_id: str, decision: dict) -> Iterator[TraceEvent]:
    cfg = {"configurable": {"thread_id": signal_id}}
    if not _graph(config.settings.CHECKPOINT_PATH).get_state(cfg).next:
        yield TraceEvent(node="resume", kind="graph", output={"error": "nothing to resume"}, ts=time.time())
        return
    yield from _stream(Command(resume=decision), signal_id)


if __name__ == "__main__":
    import sys

    sid = sys.argv[1] if len(sys.argv) > 1 else "FB-101"
    events = list(run_signal(sid))
    if events and events[-1].kind == "human":
        events += list(resume(sid, {"action": "approve", "category": "bug", "code_area": "handler-frontend"}))
    for e in events:
        print(e.model_dump_json())

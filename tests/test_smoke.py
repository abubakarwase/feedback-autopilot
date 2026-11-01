import dataclasses
import json
import shutil
from pathlib import Path
import sqlite3

import pytest

from app import config, decide as decide_mod, graph
from app.tools import github, repo
from app.mcp_client import labels_call
from app.schemas import (Evidence, FixAttempt, Issue, Label, PullRequest, ScreenCard,
                         Signal, TraceEvent, Triage)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    db = tmp_path / "seed.sqlite"
    shutil.copy(config.settings.DB_PATH, db)
    with sqlite3.connect(db) as con:
        for sid in ("FB-101", "FB-103"):
            con.execute("INSERT OR REPLACE INTO signals (id, source, text, user, channel, page_url, route, element, ts) "
                        "VALUES (?, 'slack', 'Download PDF button does nothing', 'U1', 'C1', "
                        "'/invoices/42', '/invoices/:id', '#download-pdf', 1.0)", (sid,))
    s = dataclasses.replace(config.settings, OFFLINE=True, DECIDE_BACKEND="mock", DB_PATH=str(db),
                            CHECKPOINT_PATH=str(tmp_path / "cp.sqlite"))
    monkeypatch.setattr(config, "settings", s)
    monkeypatch.setattr(decide_mod, "FIXTURES", tmp_path / "none.json")
    # keep graph tests off the real repo / PR db
    monkeypatch.setattr(repo, "create_worktree", lambda b, root=".": str(tmp_path))
    monkeypatch.setattr(repo, "run_tests", lambda wt, t=None: {"passed": True, "returncode": 1, "summary": "ok", "output": ""})
    monkeypatch.setattr(repo, "diff_stats", lambda wt: {"lines": 2, "files": ["target-app/app.js"], "protected": []})
    monkeypatch.setattr(repo, "commit_all", lambda wt, m: "abc123")
    monkeypatch.setattr(github, "DB_NAME", str(tmp_path / "gh.sqlite"))
    monkeypatch.setattr(github, "merge_pr", lambda n, merged_by="developer", root=".": {"number": n, "state": "merged"})
    graph._graph.cache_clear()
    yield s
    graph._graph.cache_clear()


def test_schemas():
    Signal(id="FB-101")
    Evidence()
    Issue(page="/invoices/:id", element="#download-pdf", expected="a", actual="b")
    t = Triage(category="bug", category_p=0.9, code_area="handler-frontend", code_area_p=0.8,
               fixable=True, fixable_p=0.7)
    assert t.p == 0.7
    FixAttempt(branch="fix/FB-101")
    PullRequest(number=1, url="u", branch="b")
    Label(signal_id="FB-101", category="bug", code_area="x", outcome="triaged_by_human")
    TraceEvent(node="n", kind="tool", ts=1.0)
    assert ScreenCard.model_fields.keys() >= {"screen", "route", "forwarder", "developer", "sources"}


def test_config_strips_comments(monkeypatch):
    monkeypatch.setenv("GITHUB_BACKEND", "mock   # mock | real")
    assert config.load().GITHUB_BACKEND == "mock"
    assert config.load().MAX_RETRIES == 2


def test_decide_mock_fixture_try(cfg, monkeypatch, tmp_path):
    f = tmp_path / "fx.json"
    f.write_text(json.dumps({"FB-103": {"q": [{"answer": "a", "p": 0.48}, {"answer": "b", "p": 0.55}]}}))
    monkeypatch.setattr(decide_mod, "FIXTURES", f)
    d = decide_mod.decide("q", ["a", "b"], {}, "FB-103", 1)
    assert (d["answer"], d["p"], d["backend"]) == ("a", 0.48, "mock")
    assert decide_mod.decide("q", ["a", "b"], {}, "FB-103", 2)["answer"] == "b"
    assert decide_mod.decide("q", ["a", "b"], {}, "FB-103", 9)["answer"] == "b"
    assert decide_mod.decide("other", ["a", "b"], {}, "FB-103")["backend"] == "offline"


def test_mcp_roundtrip():
    r = labels_call("get_trust", category="bug", code_area="handler-frontend")
    assert r["trusted"] is False and r["cases"] == 0 and r["reverts"] == 0
    assert (r["category"], r["code_area"]) == ("bug", "handler-frontend")


def test_run_and_resume(cfg):
    ev = list(graph.run_signal("FB-101"))
    assert [e.node for e in ev][:3] == ["collect_signal", "enrich", "extract_issue"]
    assert ev[-1].kind == "human" and ev[-1].node == "developer_review"
    ev2 = list(graph.resume("FB-101", {"action": "approve"}))
    assert ev2[-1].node == "merge_and_notify"


def test_low_p_triage_goes_human(cfg, monkeypatch, tmp_path):
    q = lambda a, p: [{"answer": a, "p": p}]
    f = tmp_path / "fx.json"
    f.write_text(json.dumps({"FB-103": {
        graph.Q_CATEGORY: q("ux", 0.48), graph.Q_AREA: q("frontend", 0.9),
        graph.Q_FIXABLE: q("yes", 0.9)}}))
    monkeypatch.setattr(decide_mod, "FIXTURES", f)
    ev = list(graph.run_signal("FB-103"))
    assert sum(e.node == "enrich" for e in ev) == 3  # first pass + MAX_RETRIES loops
    assert ev[-1].node == "human_triage"
    ev2 = list(graph.resume("FB-103", {"category": "bug", "code_area": "frontend"}))
    assert any(e.node == "human_triage" and e.p == 1.0 for e in ev2)


def test_repro_tests_lookup(cfg, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(repo, "run_tests", lambda wt, t=None: seen.append(t) or {"returncode": 1, "summary": "x"})
    monkeypatch.setattr(graph.shutil, "copy", lambda src, dst: seen.append(Path(src).name))
    graph.reproduce({"signal_id": "FB-101", "issue": {}, "retries": {}})
    assert "test_fb101_download_pdf.py" in seen

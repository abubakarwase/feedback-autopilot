import dataclasses
import subprocess
from pathlib import Path

import pytest

from app import coder, config
from app.gate import merge_gate
from app.schemas import FixAttempt, Issue, Triage
from app.tools import github, repo


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "target-app/tests").mkdir(parents=True)
    (r / "target-app/app.js").write_text("a\nb\nc\n")
    (r / "target-app/tests/test_old.py").write_text("def test_x():\n    assert True\n")
    (r / "data/patches").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, GITHUB_BASE_BRANCH="demo",
                                                                CODER_BACKEND="stub"))
    return str(r)


def _fix(**kw):
    d = dict(branch="fix/x", test_failed_before=True, tests_pass_after=True, lint_ok=True, diff_lines=5,
             files=["target-app/app.js"], p=0.9)
    return FixAttempt(**{**d, **kw})


TRIAGE = Triage(category="bug", category_p=0.95, code_area="a", code_area_p=0.95, fixable=True, fixable_p=0.95)


def gate(fix=None, triage=TRIAGE, trust=None, merges=0, sid="FB-102"):
    return merge_gate(fix or _fix(), triage, {"trusted": True} if trust is None else trust, sid, merges)


def test_gate_pass():
    g = gate()
    assert g["auto_merge"] and g["reasons"] == []


@pytest.mark.parametrize("name,kw", [
    ("reproduced", dict(fix=_fix(test_failed_before=False))),
    ("suite_and_lint", dict(fix=_fix(tests_pass_after=False))),
    ("diff_small", dict(fix=_fix(diff_lines=999))),
    ("diff_small", dict(fix=_fix(files=["a", "b", "c", "d"]))),
    ("no_protected_paths", dict(fix=_fix(touched_protected_paths=["x"]))),
    ("confidence", dict(triage=TRIAGE.model_copy(update={"fixable_p": 0.5}))),
    ("confidence", dict(fix=_fix(p=0.5))),
    ("trusted", dict(trust={"trusted": False})),
    ("daily_limit", dict(merges=99)),
])
def test_gate_fails(name, kw):
    g = gate(**kw)
    assert not g["auto_merge"] and name in g["reasons"]


def test_gate_spot_check(monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, SPOT_CHECK_RATE=1.0))
    assert gate()["reasons"] == ["not_spot_checked"]


def test_worktree_diff_commit(tmp_repo):
    wt = repo.create_worktree("fix/fb-1", root=tmp_repo)
    assert wt.endswith(".worktrees/fix-fb-1") and Path(wt, "target-app/app.js").exists()
    Path(wt, "target-app/app.js").write_text("a\nB\nc\n")
    Path(wt, "target-app/tests/test_repro_fb_1.py").write_text("def test_r():\n    assert False\n")
    st = repo.diff_stats(wt)
    assert st["lines"] == 2 and st["files"] == ["target-app/app.js"]
    assert not st["outside_target"] and not st["edits_existing_tests"]
    assert repo.run_tests(wt, "target-app/tests/test_repro_fb_1.py")["passed"] is False
    assert repo.run_tests(wt, "nope.py")["passed"] is False
    Path(wt, "target-app/tests/test_old.py").write_text("def test_x():\n    assert 1\n")
    Path(wt, "README").write_text("x\n")
    st = repo.diff_stats(wt)
    assert st["outside_target"] and st["edits_existing_tests"] and "README" in st["protected"]
    sha = repo.commit_all(wt, "Fix FB-1")
    assert len(sha) == 40
    wt2 = repo.create_worktree("fix/fb-1", root=tmp_repo)  # rerun: recreated clean
    assert not Path(wt2, "README").exists()


def test_search_and_read(tmp_repo):
    hits = repo.search_code("B", root=tmp_repo)
    assert hits[0]["file"] == "target-app/app.js" and hits[0]["line"] == 2
    assert repo.read_file("target-app/app.js", root=tmp_repo).startswith("a")


def test_github_open_merge_revert(tmp_repo):
    wt = repo.create_worktree("fix/fb-2", root=tmp_repo)
    Path(wt, "target-app/app.js").write_text("fixed\n")
    repo.commit_all(wt, "Fix")
    pr = github.open_pr("fix/fb-2", "t", "b", root=tmp_repo)
    assert pr["number"] == 1 and pr["url"] == "mock://pr/1"
    assert github.request_review(1, root=tmp_repo)["state"] == "needs_review"
    m = github.merge_pr(1, merged_by="auto", root=tmp_repo)
    assert m["state"] == "merged" and github.merges_today(tmp_repo) == 1
    assert Path(repo.ensure_demo_worktree(tmp_repo), "target-app/app.js").read_text() == "fixed\n"
    github.revert_pr(1, root=tmp_repo)
    assert Path(repo.ensure_demo_worktree(tmp_repo), "target-app/app.js").read_text() == "a\nb\nc\n"
    assert github.create_issue("t", "b", root=tmp_repo)["url"] == "mock://issue/1"
    github.reset_mock(tmp_repo)
    assert github.merges_today(tmp_repo) == 0


def test_coder_stub(tmp_repo):
    wt = repo.create_worktree("fix/fb-3", root=tmp_repo)
    issue = Issue(page="p", element="e", expected="x", actual="y")
    assert coder.code_fix(issue, [], wt, "FB-3", root=tmp_repo) == {"ok": False, "summary": "no stub patch", "steps": 0}
    patch = Path(tmp_repo, "data/patches/FB-3.diff")
    patch.write_text("--- a/target-app/app.js\n+++ b/target-app/app.js\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n")
    assert coder.code_fix(issue, [], wt, "FB-3", root=tmp_repo)["ok"]
    assert Path(wt, "target-app/app.js").read_text() == "a\nB\nc\n"


def test_diff_stats_protects_tests_and_config(tmp_repo):
    wt = repo.create_worktree("fix/fb-4", root=tmp_repo)
    Path(wt, "target-app/tests/test_repro_fb_4.py").write_text("x\n")
    assert repo.diff_stats(wt)["protected"] == []
    Path(wt, "target-app/tests/test_sneaky.py").write_text("x\n")
    Path(wt, "target-app/conftest.py").write_text("x\n")
    Path(wt, "target-app/pytest.ini").write_text("x\n")
    assert repo.diff_stats(wt)["protected"] == ["target-app/conftest.py", "target-app/pytest.ini",
                                                "target-app/tests/test_sneaky.py"]


def test_verify_fails_when_repro_test_changed(tmp_repo, monkeypatch):
    from app import graph
    wt = repo.create_worktree("fix/fb-5", root=tmp_repo)
    rel = "target-app/tests/test_repro_fb_5.py"
    Path(wt, rel).write_text("def test_r():\n    assert True\n")
    s = {"signal_id": "FB-5", "worktree": wt, "retries": {}, "issue": {"page": "p"},
         "fix": {"branch": "fix/fb-5", "test_path": rel, "test_failed_before": True}}
    import hashlib
    good = hashlib.sha256(Path(wt, rel).read_bytes()).hexdigest()
    monkeypatch.setattr(repo, "commit_all", lambda w, m: "sha")
    assert graph.verify({**s, "repro_sha": good})["fix"]["tests_pass_after"] is True
    assert graph.verify({**s, "repro_sha": "0" * 64})["fix"]["tests_pass_after"] is False


def test_diff_stats_ignores_pyc(tmp_repo):
    wt = repo.create_worktree("fix/fb-6", root=tmp_repo)
    Path(wt, "target-app/tests/__pycache__").mkdir()
    Path(wt, "target-app/tests/__pycache__/x.cpython-312.pyc").write_bytes(b"\0")
    Path(wt, "target-app/y.pyc").write_bytes(b"\0")
    st = repo.diff_stats(wt)
    assert st["all_files"] == [] and st["protected"] == []

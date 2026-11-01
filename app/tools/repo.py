"""Repo tools: code search, file read, worktrees, tests, diff stats, commit.

All functions take an optional `root` (the git repo root, default cwd) so they can be tested on a tmp repo.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from app import config

TARGET = "target-app"
TESTS = f"{TARGET}/tests"
PROTECTED_RE = re.compile(r"billing|auth|migration|\.github|\.gitlab-ci|Jenkinsfile", re.I)
CONFIG_NAMES = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", ".pytest.ini"}
NO_PYC = [":(exclude)**/__pycache__/**", ":(exclude)*.pyc"]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".worktrees", "test-results"}


def _git(cwd, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def _has_ref(root, ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=str(root),
                          capture_output=True).returncode == 0


def _base(root) -> str:
    """Ensure the local base branch exists (from origin/<base> if present, else main); return its name."""
    base = config.settings.GITHUB_BASE_BRANCH
    if not _has_ref(root, f"refs/heads/{base}"):
        src = f"origin/{base}" if _has_ref(root, f"refs/remotes/origin/{base}") else "main"
        _git(root, "branch", base, src)
    return base


def _wt_path(root, name: str) -> Path:
    return Path(root).resolve() / ".worktrees" / name.replace("/", "-")


def search_code(query: str, path: str = TARGET, root: str = ".") -> list[dict]:
    """Case-insensitive search for each term of `query` under `path`; max 30 matches."""
    terms = [t.lower() for t in query.split() if t]
    out: list[dict] = []
    base = Path(root) / path
    for dp, dns, fns in os.walk(base):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            f = Path(dp) / fn
            try:
                lines = f.read_text().splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            rel = f.relative_to(root).as_posix()
            for i, line in enumerate(lines, 1):
                if any(t in line.lower() for t in terms):
                    out.append({"file": rel, "line": i, "text": line.strip()})
                    if len(out) >= 30:
                        return out
    return out


def read_file(path: str, root: str = ".") -> str:
    """Return the text of a file."""
    return (Path(root) / path).read_text()


def create_worktree(branch: str, root: str = ".") -> str:
    """Fresh worktree .worktrees/<branch> on a new `branch` cut from the base branch; return its path."""
    base = _base(root)
    wt = _wt_path(root, branch)
    if wt.exists():
        _git(root, "worktree", "remove", "--force", str(wt), check=False)
    _git(root, "worktree", "prune")
    if _has_ref(root, f"refs/heads/{branch}"):
        _git(root, "branch", "-D", branch)
    _git(root, "worktree", "add", str(wt), "-b", branch, base)
    return str(wt)


def ensure_demo_worktree(root: str = ".") -> str:
    """Worktree .worktrees/<base> checked out on the base branch (target of merges); return its path."""
    base = _base(root)
    wt = _wt_path(root, base)
    if not (wt / ".git").exists():
        _git(root, "worktree", "prune")
        _git(root, "worktree", "add", str(wt), base)
    return str(wt)


def run_tests(worktree: str, test_path: str | None = None) -> dict:
    """Run pytest (one path or the whole target-app/tests) with cwd=worktree."""
    target = test_path or TESTS
    if not (Path(worktree) / target).exists():
        return {"passed": False, "returncode": -1, "summary": f"test path not found: {target}", "output": ""}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", target, "-q", "-x", "--no-header",
                            "-p", "no:cacheprovider"], cwd=worktree, capture_output=True, text=True, timeout=180,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": -1, "summary": "timeout after 180 s", "output": ""}
    out = (r.stdout + r.stderr).strip()
    return {"passed": r.returncode == 0, "returncode": r.returncode,
            "summary": out.splitlines()[-1] if out else "", "output": out[-3000:]}


def diff_stats(worktree: str) -> dict:
    """Diff of the worktree against the base branch (staged, includes untracked files).

    `lines`/`files` do not count newly added test files (the repro test is not part of the fix size).
    """
    base = config.settings.GITHUB_BASE_BRANCH
    _git(worktree, "add", "-A", "--", ".", *NO_PYC)
    status = dict(reversed(l.split("\t", 1)) for l in
                  _git(worktree, "diff", "--cached", "--no-renames", "--name-status", base).splitlines())
    lines, files, all_files = 0, [], list(status)
    for l in _git(worktree, "diff", "--cached", "--no-renames", "--numstat", base).splitlines():
        add, dele, name = l.split("\t", 2)
        if status.get(name) == "A" and name.startswith(TESTS + "/"):
            continue
        files.append(name)
        lines += (int(add) if add.isdigit() else 0) + (int(dele) if dele.isdigit() else 0)
    outside = any(not f.startswith(TARGET + "/") for f in all_files)
    edits_tests = any(f.startswith(TESTS + "/") and st != "A" for f, st in status.items())
    protected = [f for f in all_files if not f.startswith(TARGET + "/") or PROTECTED_RE.search(f)]
    for f, st in status.items():
        name = f.rsplit("/", 1)[-1]
        if name.lower() in CONFIG_NAMES or (f.startswith(TESTS + "/") and (st != "A" or not name.startswith("test_repro_"))):
            protected.append(f)
    return {"lines": lines, "files": files, "all_files": all_files, "outside_target": outside,
            "edits_existing_tests": edits_tests, "protected": sorted(set(protected))}


def commit_all(worktree: str, message: str) -> str:
    """git add -A && git commit; return the commit sha."""
    _git(worktree, "add", "-A", "--", ".", *NO_PYC)
    _git(worktree, "commit", "-m", message)
    return _git(worktree, "rev-parse", "HEAD")

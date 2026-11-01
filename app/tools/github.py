"""GitHub tools. GITHUB_BACKEND=mock keeps PRs/issues in SQLite (.github_mock.sqlite at repo root)."""
import sqlite3
from datetime import date, datetime
from pathlib import Path

from app import config
from app.tools import repo

DB_NAME = ".github_mock.sqlite"


def _db(root: str) -> sqlite3.Connection:
    c = sqlite3.connect(Path(root) / DB_NAME)
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE IF NOT EXISTS prs(number INTEGER PRIMARY KEY AUTOINCREMENT, branch, title, body,"
        " state, created_at, merged_by, merge_sha, merged_at);"
        "CREATE TABLE IF NOT EXISTS issues(number INTEGER PRIMARY KEY AUTOINCREMENT, title, body, created_at);")
    return c


def _pr(c, number: int) -> dict:
    row = c.execute("SELECT * FROM prs WHERE number=?", (number,)).fetchone()
    if not row:
        raise KeyError(f"PR {number} not found")
    return {**dict(row), "url": f"mock://pr/{number}"}


def open_pr(branch: str, title: str, body: str, root: str = ".") -> dict:
    """Open a PR from `branch` against GITHUB_BASE_BRANCH."""
    with _db(root) as c:
        n = c.execute("INSERT INTO prs(branch,title,body,state,created_at) VALUES(?,?,?,?,?)",
                      (branch, title, body, "needs_review", datetime.now().isoformat())).lastrowid
    return {"number": n, "url": f"mock://pr/{n}", "branch": branch, "state": "needs_review"}


def merge_pr(number: int, merged_by: str = "developer", root: str = ".") -> dict:
    """Merge the PR branch into the base branch (in the demo worktree)."""
    with _db(root) as c:
        pr = _pr(c, number)
        wt = repo.ensure_demo_worktree(root)
        try:
            repo._git(wt, "merge", "--no-ff", pr["branch"], "-m", f"Merge PR #{number}")
        except RuntimeError:
            repo._git(wt, "merge", "--abort", check=False)
            raise
        sha = repo._git(wt, "rev-parse", "HEAD")
        c.execute("UPDATE prs SET state='merged', merged_by=?, merge_sha=?, merged_at=? WHERE number=?",
                  (merged_by, sha, datetime.now().isoformat(), number))
        return _pr(c, number)


def request_review(number: int, root: str = ".") -> dict:
    """Ask a developer to review a PR."""
    with _db(root) as c:
        c.execute("UPDATE prs SET state='needs_review' WHERE number=?", (number,))
        return _pr(c, number)


def create_issue(title: str, body: str, root: str = ".") -> dict:
    """Create a GitHub issue."""
    with _db(root) as c:
        n = c.execute("INSERT INTO issues(title,body,created_at) VALUES(?,?,?)",
                      (title, body, datetime.now().isoformat())).lastrowid
    return {"number": n, "url": f"mock://issue/{n}", "title": title}


def revert_pr(number: int, root: str = ".") -> dict:
    """Revert a merged PR on the base branch."""
    with _db(root) as c:
        pr = _pr(c, number)
        if not pr["merge_sha"]:
            raise RuntimeError(f"PR {number} is not merged")
        wt = repo.ensure_demo_worktree(root)
        repo._git(wt, "revert", "-m", "1", "--no-edit", pr["merge_sha"])
        c.execute("UPDATE prs SET state='reverted' WHERE number=?", (number,))
        return {**_pr(c, number), "reverts": number}


def merges_today(root: str = ".") -> int:
    """Number of auto-merged PRs today (for the merge gate)."""
    with _db(root) as c:
        return c.execute("SELECT COUNT(*) FROM prs WHERE state='merged' AND merged_by='auto'"
                         " AND merged_at LIKE ?", (date.today().isoformat() + "%",)).fetchone()[0]


def reset_mock(root: str = ".") -> None:
    """Delete the mock database."""
    (Path(root) / DB_NAME).unlink(missing_ok=True)

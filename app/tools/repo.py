"""Repo tools: code search, file read, worktree, tests, diff stats."""


def search_code(query: str, path: str = "target-app") -> list[dict]:
    """Search the code under `path`; return matches with file and line."""
    # STUB: replaced by Abubakar
    return [{"file": "target-app/app.js", "line": 42, "text": "document.getElementById('download-pdf-btn')"}]


def read_file(path: str) -> str:
    """Return the text of a file."""
    # STUB: replaced by Abubakar
    return "document.getElementById('download-pdf-btn').addEventListener('click', downloadPdf);\n"


def create_worktree(branch: str) -> str:
    """Create a git worktree for `branch`; return its path."""
    # STUB: replaced by Abubakar
    return f"/tmp/worktrees/{branch}"


def run_tests(worktree: str, test_path: str | None = None) -> dict:
    """Run tests (one file or full suite) in a worktree."""
    # STUB: replaced by Abubakar
    return {"passed": False if test_path else True, "failed": 1 if test_path else 0,
            "output": "TypeError: handler cannot find #download-pdf", "lint_ok": True}


def diff_stats(worktree: str) -> dict:
    """Return changed files and diff line count."""
    # STUB: replaced by Abubakar
    return {"files": ["target-app/app.js"], "lines": 2, "protected": []}

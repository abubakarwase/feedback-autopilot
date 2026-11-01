"""GitHub tools. GITHUB_BACKEND=mock keeps PRs in SQLite; real wraps the gh CLI."""


def open_pr(branch: str, title: str, body: str) -> dict:
    """Open a PR from `branch` against GITHUB_BASE_BRANCH."""
    # STUB: replaced by Abubakar
    return {"number": 1, "url": "https://github.com/abubakarwase/feedback-autopilot/pull/1",
            "branch": branch, "state": "needs_review"}


def merge_pr(number: int) -> dict:
    """Merge a PR."""
    # STUB: replaced by Abubakar
    return {"number": number, "state": "merged"}


def request_review(number: int) -> dict:
    """Ask a developer to review a PR."""
    # STUB: replaced by Abubakar
    return {"number": number, "state": "needs_review"}


def create_issue(title: str, body: str) -> dict:
    """Create a GitHub issue."""
    # STUB: replaced by Abubakar
    return {"number": 2, "url": "https://github.com/abubakarwase/feedback-autopilot/issues/2", "title": title}


def revert_pr(number: int) -> dict:
    """Open a revert PR for a merged PR."""
    # STUB: replaced by Abubakar
    return {"number": 3, "url": "https://github.com/abubakarwase/feedback-autopilot/pull/3", "reverts": number}

"""Four earlier copy · frontend fixes a developer approved unchanged: signals + run history rows.

They reuse existing labels from labels_fixture (same signal_id), so label counts and trust stay the same.
Ids are below FB-101, so app/api.py still numbers live posts FB-101, FB-102, FB-103.
"""
import json
from datetime import datetime, timedelta, timezone

BASE = "http://localhost:8000/app/"

# Same schema as ui/run_history.py.
RUNS_SQL = """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    signal_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('run', 'resume')),
    started_at REAL NOT NULL,
    ended_at REAL,
    outcome TEXT,
    events TEXT NOT NULL DEFAULT '[]'
)"""

# signal_id: (user, text, hash route, file, title, diff_lines, pr number)
FIXES = {
    "FB-046": ("u-dana", "Booking form says 'Pices' instead of 'Pieces'", "#/booking",
               "target-app/app.js", "Fix typo 'Pices' in booking form label", 2, 41),
    "FB-018": ("u-emil", "Invoice total label reads 'Totla'", "#/invoices/INV-2187",
               "target-app/app.js", "Fix typo 'Totla' in invoice total label", 2, 42),
    "FB-007": ("u-fatima", "Status 'In transit' gets cut off in the list", "#/shipments",
               "target-app/styles.css", "Stop truncating the 'In transit' status badge", 4, 43),
    "FB-090": ("u-gregor", "Date hint on booking shows MM/DD/YYYY, we use DD.MM.YYYY", "#/booking",
               "target-app/app.js", "Show DD.MM.YYYY as booking date hint", 2, 44),
}
CHECKS = {"reproduced": True, "suite_and_lint": True, "diff_small": True, "no_protected_paths": True,
          "confidence": True, "trusted": False, "not_spot_checked": True, "daily_limit": True}


def _route(hash_path: str) -> str:
    parts = hash_path.lstrip("#/").split("/")
    return "/" + "/".join(":id" if p.startswith("INV-") else p for p in parts)


def _events(sid: str, fix: tuple, t0: float) -> tuple[list[dict], list[dict]]:
    user, text, path, file, title, diff_lines, pr_no = fix
    url, route = BASE + path, _route(path)
    branch, test = f"fix/{sid}", f"target-app/tests/test_{sid.lower().replace('-', '')}.py"
    pr = {"number": pr_no, "url": f"https://github.com/demo/target-app/pull/{pr_no}", "branch": branch,
          "state": "needs_review", "gate_result": {"auto_merge": False, "reasons": ["trusted"], "checks": CHECKS},
          "merged_by": None}
    steps = [
        ("collect_signal", "tool", sid, {"id": sid, "source": "feedback", "text": text, "user": user,
                                         "page_url": url}, None, 1),
        ("enrich", "tool", url, {"route": route, "rage_clicks": 0, "dead_clicks": 0, "console_errors": 0}, None, 2),
        ("extract_issue", "llm", text, {"page": route, "actual": text, "candidate_files": [file]}, None, 4),
        ("triage", "decision", {"text": text, "page": route},
         {"category": "copy", "code_area": "frontend", "fixable": True, "duplicate_of": None,
          "route": "locate_code"}, 0.94, 3),
        ("locate_code", "tool", [file], {"files": [file], "hits": 1}, None, 1),
        ("reproduce", "llm", {"page": route, "actual": text}, {"test": test, "failed_before": True}, None, 9),
        ("fix", "llm", [file], {"files": [file], "diff_lines": diff_lines}, 0.91, 14),
        ("verify", "tool", branch, {"tests_pass_after": True, "lint_ok": True, "diff_lines": diff_lines,
                                    "files": [file]}, None, 11),
        ("open_pr", "tool", branch, pr, None, 2),
        ("merge_gate", "python", {"branch": branch, "diff_lines": diff_lines},
         {**pr["gate_result"], "route": "developer_review"}, None, 1),
        ("developer_review", "human", None, {"node": "developer_review", "signal_id": sid, "pr": pr}, None, 0),
    ]
    run, t = [], t0
    for node, kind, inp, out, p, dt in steps:
        t += dt
        run.append({"node": node, "kind": kind, "input": inp, "output": out, "p": p, "retry": 0, "ts": t})
    return run, [{"title": title, "pr": pr}]


def build(labels: list[dict]) -> tuple[list[dict], list[dict]]:
    """Signal rows and run rows for FIXES. Raise if a reused label is not a merged copy · frontend case."""
    by_sid = {l["signal_id"]: l for l in labels}
    signals, runs = [], []
    for sid, fix in FIXES.items():
        label = by_sid.get(sid)
        if label is None or (label["category"], label["code_area"], label["outcome"]) != \
                ("copy", "frontend", "merged_unchanged"):
            raise ValueError(f"{sid}: needs an existing copy · frontend merged_unchanged label, got {label}")
        merged = datetime.fromisoformat(label["created_at"]).replace(tzinfo=timezone.utc)
        reported = merged - timedelta(hours=5)
        user, text, path = fix[0], fix[1], fix[2]
        signals.append({"id": sid, "source": "feedback", "text": text, "user": user, "channel": "C-feedback",
                        "file": "", "page_url": BASE + path, "route": _route(path), "element": "",
                        "stats": json.dumps({}), "ts": reported.timestamp(), "created_at": reported.isoformat()})

        t0 = (reported + timedelta(minutes=2)).timestamp()
        run, (meta,) = _events(sid, fix, t0)
        runs.append({"signal_id": sid, "kind": "run", "started_at": t0, "ended_at": run[-1]["ts"],
                     "outcome": "Waiting for developer review", "events": json.dumps(run)})

        t1 = (merged - timedelta(minutes=1)).timestamp()
        pr = {**meta["pr"], "state": "merged", "merged_by": "developer"}
        resume = [
            {"node": "developer_review", "kind": "human", "input": {"action": "approve"}, "output": {"action": "approve"},
             "p": None, "retry": 0, "ts": t1 + 1},
            {"node": "merge_and_notify", "kind": "tool", "input": pr["number"],
             "output": {"merge": pr, "reply": f"Thanks {user}, fixed: {meta['title']}.", "by": "developer"},
             "p": None, "retry": 0, "ts": t1 + 4},
        ]
        runs.append({"signal_id": sid, "kind": "resume", "started_at": t1, "ended_at": t1 + 4,
                     "outcome": "Merged after review", "events": json.dumps(resume)})
    return signals, runs

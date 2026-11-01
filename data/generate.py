"""Generate demo data: PostHog event fixtures, seed.sqlite with past labels. Stdlib only, seed 42."""
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent
sys.path.insert(0, str(DATA))

import labels_fixture  # noqa: E402
import posthog_fixture  # noqa: E402

SEED = 42
REQUIRED_SIGNALS = ["FB-101", "FB-102", "FB-103", "HM-shipments-eta-cell", "HM-invoices-download-pdf"]
TABLES = {"labels", "trust", "signals", "replies", "eval_runs", "impact_checks", "screen_cards"}
# Must match the question strings and options in app/graph.py.
QUESTIONS = {
    "Which category is this feedback?": {"bug", "copy", "ux", "feature_request", "how_to"},
    "Which code area does this belong to?": {"handler-frontend", "frontend", "backend"},
    "Can an agent fix this with a small code change?": {"yes", "no"},
}


def write_json(path, events):
    if not events:
        raise ValueError(f"no events generated for {path.name}")
    path.write_text(json.dumps(events, indent=1) + "\n")


def write_db(path, labels):
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(labels_fixture.SCHEMA_SQL)
        con.executemany(
            "INSERT INTO labels (id, signal_id, category, code_area, outcome, triage_corrections, created_at) "
            "VALUES (:id, :signal_id, :category, :code_area, :outcome, :triage_corrections, :created_at)",
            labels,
        )
        con.commit()
        n_labels = con.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        n_trust = con.execute("SELECT COUNT(*) FROM trust").fetchone()[0]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()
    if n_labels != len(labels) or n_trust != 0:
        raise RuntimeError(f"seed.sqlite has {n_labels} labels (expected {len(labels)}), {n_trust} trust rows")
    if TABLES - tables:
        raise RuntimeError(f"seed.sqlite is missing tables: {sorted(TABLES - tables)}")
    return n_labels


def check_decide_fixtures(path):
    fixtures = json.loads(path.read_text())
    missing = [s for s in REQUIRED_SIGNALS if s not in fixtures]
    if missing:
        raise KeyError(f"{path.name} is missing signals: {missing}")
    for sid, questions in fixtures.items():
        if sid.startswith("_"):
            continue
        if set(questions) != set(QUESTIONS):
            raise KeyError(f"{path.name} {sid}: questions {sorted(questions)} != {sorted(QUESTIONS)}")
        for q, tries in questions.items():
            bad = [t for t in tries if t["answer"] not in QUESTIONS[q] or not 0 <= t["p"] <= 1]
            if not tries or bad:
                raise ValueError(f"{path.name} {sid} / {q!r}: invalid tries {tries}")
    return len([k for k in fixtures if not k.startswith("_")])


def main():
    events = posthog_fixture.build_events(random.Random(SEED))
    after = posthog_fixture.build_after_fix_events(random.Random(SEED))
    labels = labels_fixture.build_labels(random.Random(SEED))

    write_json(DATA / "posthog_events.json", events)
    write_json(DATA / "posthog_events_after_fix.json", after)
    # DB_PATH (env, relative to the repo root) lets tests build a throwaway DB.
    db_path = Path(os.environ.get("DB_PATH") or DATA / "seed.sqlite")
    n_labels = write_db(db_path, labels)
    n_signals = check_decide_fixtures(DATA / "decide_fixtures.json")

    print(f"posthog_events.json: {len(events)} events")
    print(f"posthog_events_after_fix.json: {len(after)} events")
    print(f"{db_path}: {n_labels} labels, 0 trust rows, tables {', '.join(sorted(TABLES))}")
    print(f"decide_fixtures.json: {n_signals} signals OK")


if __name__ == "__main__":
    main()

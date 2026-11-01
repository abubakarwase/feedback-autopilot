"""Past developer decisions (150 labels) for the seed DB. data/generate.py writes them."""
import json
import random
from datetime import datetime, timedelta

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    signal_id TEXT,
    category TEXT,
    code_area TEXT,
    outcome TEXT,
    triage_corrections TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS trust (
    category TEXT,
    code_area TEXT,
    cases INTEGER,
    merged_unchanged INTEGER,
    reverts INTEGER,
    trusted INTEGER,
    updated_at TEXT,
    PRIMARY KEY (category, code_area)
);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    source TEXT,
    text TEXT,
    user TEXT,
    channel TEXT,
    file TEXT,
    page_url TEXT,
    route TEXT,
    element TEXT,
    stats TEXT,
    ts REAL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    signal_id TEXT,
    text TEXT,
    ts REAL
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY,
    ts REAL,
    scores TEXT
);
CREATE TABLE IF NOT EXISTS impact_checks (
    id INTEGER PRIMARY KEY,
    signal_id TEXT,
    route TEXT,
    element TEXT,
    before INTEGER,
    after INTEGER,
    verdict TEXT,
    ts REAL
);
CREATE TABLE IF NOT EXISTS screen_cards (
    screen TEXT PRIMARY KEY,
    route TEXT,
    card TEXT,
    version INTEGER,
    updated_at TEXT
);
"""

TODAY = datetime(2026, 11, 1)
OUTCOMES = ["merged_unchanged", "edited", "rejected", "reverted"]

# Every combo other than copy · frontend stays below 30 cases, so none can pass the trust gate.
OTHER_COMBOS = [
    ("bug", "shipments", 14), ("bug", "invoice", 12), ("bug", "booking", 10),
    ("bug", "api", 10), ("ux", "shipments", 12), ("ux", "frontend", 8),
    ("copy", "invoice", 6), ("feature_request", "booking", 8),
    ("feature_request", "billing", 6), ("how_to", "auth", 6), ("bug", "billing", 6),
]
HANDLER_OUTCOMES = ["merged_unchanged"] * 7 + ["edited"] * 3 + ["rejected", "reverted"]


def build_labels(rng):
    rows = [("copy", "frontend", "merged_unchanged") for _ in range(40)]
    rows += [("bug", "handler-frontend", o) for o in HANDLER_OUTCOMES]
    others = [
        (cat, area, rng.choices(OUTCOMES, weights=[60, 25, 10, 5])[0])
        for cat, area, n in OTHER_COMBOS for _ in range(n)
    ]
    if not any(o == "reverted" for _, _, o in others):
        cat, area, _ = others[0]
        others[0] = (cat, area, "reverted")
    rows += others
    rng.shuffle(rows)

    times = sorted(TODAY - timedelta(seconds=rng.randint(3600, 60 * 86400)) for _ in rows)
    signal_ids = [f"FB-{i:03d}" for i in range(1, 134) if i not in (101, 102, 103)] + [f"HM-old-{i:02d}" for i in range(1, 21)]
    rng.shuffle(signal_ids)
    return [
        {
            "id": i + 1,
            "signal_id": signal_ids[i],
            "category": cat,
            "code_area": area,
            "outcome": outcome,
            "triage_corrections": json.dumps({}),
            "created_at": times[i].isoformat(),
        }
        for i, (cat, area, outcome) in enumerate(rows)
    ]


if __name__ == "__main__":
    from collections import Counter

    labels = build_labels(random.Random(42))
    assert len(labels) == 150
    assert not {l["signal_id"] for l in labels} & {"FB-101", "FB-102", "FB-103"}
    assert len({l["signal_id"] for l in labels}) == 150

    combos = {}
    for l in labels:
        combos.setdefault((l["category"], l["code_area"]), Counter())[l["outcome"]] += 1
    copy_fe = combos[("copy", "frontend")]
    assert copy_fe == Counter(merged_unchanged=40), copy_fe
    assert sum(combos[("bug", "handler-frontend")].values()) == 12
    assert sum(c["reverted"] for k, c in combos.items() if k != ("copy", "frontend")) >= 1

    for key, c in combos.items():
        n = sum(c.values())
        trusted = n >= 30 and c["merged_unchanged"] / n >= 0.95 and c["reverted"] == 0
        assert trusted == (key == ("copy", "frontend")), (key, c)

    start, end = TODAY - timedelta(days=60), TODAY
    assert all(start <= datetime.fromisoformat(l["created_at"]) < end for l in labels)

    for (cat, area), c in sorted(combos.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"{cat:>16} · {area:<17} n={sum(c.values()):>3}  {dict(c)}")
    print(f"OK: {len(labels)} labels, {labels[0]['created_at']} .. {labels[-1]['created_at']}")

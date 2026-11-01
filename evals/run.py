"""Nightly evals: replay all labels, score triage and merges, rebuild the trust table.

Run: uv run python evals/run.py  (or python -m evals.run)
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

MERGE_OUTCOMES = ("merged_unchanged", "edited", "rejected", "reverted")
ALL_OUTCOMES = MERGE_OUTCOMES + ("triaged_by_human",)


def _connect() -> sqlite3.Connection:
    path = config.settings.DB_PATH
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} not found; run: uv run python data/generate.py")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"labels", "trust", "eval_runs"} - have
    if missing:
        con.close()
        raise RuntimeError(f"{path} is missing tables {sorted(missing)}; run: uv run python data/generate.py")
    return con


def run() -> dict:
    s = config.settings
    con = _connect()
    try:
        labels = con.execute("SELECT * FROM labels ORDER BY id").fetchall()
        if not labels:
            raise RuntimeError("labels table is empty; run: uv run python data/generate.py")

        combos: dict[tuple[str, str], dict] = {}
        triage_ok = 0
        for l in labels:
            if l["outcome"] not in ALL_OUTCOMES:
                raise ValueError(f"label {l['id']} ({l['signal_id']}) has unknown outcome {l['outcome']!r}")
            if not json.loads(l["triage_corrections"] or "{}"):
                triage_ok += 1
            if l["outcome"] not in MERGE_OUTCOMES:
                continue
            c = combos.setdefault((l["category"], l["code_area"]), {"cases": 0, "merged_unchanged": 0, "reverts": 0})
            c["cases"] += 1
            c["merged_unchanged"] += l["outcome"] == "merged_unchanged"
            c["reverts"] += l["outcome"] == "reverted"

        now = datetime.now(timezone.utc).isoformat()
        for c in combos.values():
            c["trusted"] = (c["cases"] >= s.TRUST_MIN_CASES
                            and c["merged_unchanged"] / c["cases"] >= s.TRUST_MIN_UNCHANGED
                            and c["reverts"] == 0)
        con.execute("DELETE FROM trust")
        con.executemany(
            "INSERT INTO trust (category, code_area, cases, merged_unchanged, reverts, trusted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(cat, area, c["cases"], c["merged_unchanged"], c["reverts"], int(c["trusted"]), now)
             for (cat, area), c in combos.items()],
        )

        cases = sum(c["cases"] for c in combos.values())
        scores = {
            "labels": len(labels),
            "triage_accuracy": triage_ok / len(labels),
            "cases": cases,
            "merged_unchanged_rate": sum(c["merged_unchanged"] for c in combos.values()) / cases if cases else 0.0,
            "revert_rate": sum(c["reverts"] for c in combos.values()) / cases if cases else 0.0,
            "trusted": sorted(f"{cat} · {area}" for (cat, area), c in combos.items() if c["trusted"]),
            "combos": {f"{cat} · {area}": c for (cat, area), c in sorted(combos.items())},
        }
        ts = time.time()
        cur = con.execute("INSERT INTO eval_runs (ts, scores) VALUES (?, ?)", (ts, json.dumps(scores)))
        con.commit()
    finally:
        con.close()
    return {"id": cur.lastrowid, "ts": ts, "scores": scores}


def _print_summary(result: dict) -> None:
    sc = result["scores"]
    print(f"{'category · code area':<34} {'cases':>5} {'unchanged':>9} {'reverts':>7}  trusted")
    for name, c in sorted(sc["combos"].items(), key=lambda kv: (-kv[1]["trusted"], -kv[1]["cases"])):
        rate = c["merged_unchanged"] / c["cases"]
        print(f"{name:<34} {c['cases']:>5} {rate:>9.0%} {c['reverts']:>7}  {'yes' if c['trusted'] else 'no'}")
    print(f"\nlabels {sc['labels']}  triage accuracy {sc['triage_accuracy']:.0%}  cases {sc['cases']}  "
          f"merged unchanged {sc['merged_unchanged_rate']:.0%}  reverts {sc['revert_rate']:.1%}")
    print(f"trusted: {', '.join(sc['trusted']) or 'none'}  (eval run #{result['id']})")


if __name__ == "__main__":
    _print_summary(run())

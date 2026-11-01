"""Seeded earlier fixes (data/demo_history.py) must not change the live demo."""
import dataclasses
import hashlib
import importlib.util
import json
import random
import sqlite3
import time
from collections import Counter
from pathlib import Path

import pytest

from app import config, graph
from app.mcp_client import labels_call
from app.tools import posthog
from app.tools.labels import is_duplicate

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("generate", ROOT / "data" / "generate.py")
generate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate)
demo_history = generate.demo_history

BASE = "http://localhost:8000/app/"
DEMO = {
    "FB-101": ("u-anna", "Download button does nothing", BASE + "#/invoices/INV-2291"),
    "FB-102": ("u-ben", "Weight column says lbs, but these are kg", BASE + "#/shipments"),
    "FB-103": ("u-carla", "the shipments page is weird", BASE + "#/shipments"),
}
SEEDED = set(demo_history.FIXES)


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "t.sqlite"
    generate.write_db(path, generate.labels_fixture.build_labels(random.Random(generate.SEED)))
    s = dataclasses.replace(config.settings, OFFLINE=True, DECIDE_BACKEND="mock", DB_PATH=str(path),
                            CHECKPOINT_PATH=str(tmp_path / "cp.sqlite"))
    monkeypatch.setattr(config, "settings", s)
    graph._graph.cache_clear()
    yield path
    graph._graph.cache_clear()


def test_seeded_rows(db):
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        labels = [dict(r) for r in con.execute("SELECT * FROM labels")]
        signals = {r["id"]: dict(r) for r in con.execute("SELECT * FROM signals")}
        runs = [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id")]
    assert len(labels) == 150
    combos = Counter((l["category"], l["code_area"]) for l in labels)
    assert combos[("copy", "frontend")] == 40
    by_sid = {l["signal_id"]: l for l in labels}
    assert set(signals) == SEEDED
    for sid, s in signals.items():
        assert s["source"] == "feedback" and s["ts"] < time.time()
        assert by_sid[sid]["outcome"] == "merged_unchanged"
    assert [(r["signal_id"], r["kind"]) for r in runs] == [(s, k) for s in demo_history.FIXES for k in ("run", "resume")]
    for r in runs:
        ev = json.loads(r["events"])
        assert r["started_at"] <= ev[0]["ts"] and ev[-1]["ts"] <= r["ended_at"]
        assert all(set(e) == {"node", "kind", "input", "output", "p", "retry", "ts"} for e in ev)
        if r["kind"] == "run":
            assert [e["node"] for e in ev][-2:] == ["merge_gate", "developer_review"]
            gate = ev[-2]["output"]
            assert gate["route"] == "developer_review" and gate["reasons"] == ["trusted"]
        else:
            assert r["outcome"] == "Merged after review"
            assert ev[0]["input"] == {"action": "approve"} and ev[-1]["output"]["by"] == "developer"


def test_live_ids_start_at_fb101(db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import api

    monkeypatch.setattr(api, "SCREENSHOT_DIR", tmp_path / "shots")
    client = TestClient(api.app)
    for sid, (user, text, url) in DEMO.items():
        r = client.post("/feedback", json={"type": "message", "channel": "C-FEEDBACK", "user": user, "text": text,
                                           "ts": str(time.time()), "files": [], "page_url": url, "client_ts": ""})
        assert r.status_code == 200 and r.json()["id"] == sid


def test_no_duplicates_with_demo(db):
    from app import heatmap

    seeded = [{"text": t, "route": posthog.route_of(BASE + p), "element": ""}
              for _, t, p, *_ in demo_history.FIXES.values()]
    for _, text, url in DEMO.values():
        assert labels_call("find_duplicates", text=text, page_url=url) == []
        me = {"text": text, "route": posthog.route_of(url), "element": ""}
        assert not any(is_duplicate(me, s) for s in seeded)
    groups = heatmap.score_groups(heatmap.group_events(heatmap._windows(posthog.load_events(), posthog.now())[0]), {})
    for g in groups:
        sid = heatmap.signal_id_of(g)
        hm = {"text": heatmap.describe(g, sid), "route": g["route"], "element": g["element"]}
        assert not any(is_duplicate(hm, s) for s in seeded), sid


def test_scan_unchanged(db):
    from app import heatmap

    assert [s["id"] for s in heatmap.scan()] == ["HM-shipments-eta-cell", "HM-invoices-download-pdf"]


def test_generate_deterministic(tmp_path):
    digests = []
    for name in ("a.sqlite", "b.sqlite"):
        path = tmp_path / name
        generate.write_db(path, generate.labels_fixture.build_labels(random.Random(generate.SEED)))
        with sqlite3.connect(path) as con:
            dump = "\n".join(con.iterdump())
        digests.append(hashlib.sha256(dump.encode()).hexdigest())
    assert digests[0] == digests[1]

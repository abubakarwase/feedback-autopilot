"""Seed has no invented history; live demo posts are numbered FB-101.. and are not duplicates of each other."""
import dataclasses
import hashlib
import importlib.util
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

BASE = "http://localhost:8000/app/"
DEMO = {
    "FB-101": ("u-anna", "Download button does nothing", BASE + "#/invoices/INV-2291"),
    "FB-102": ("u-ben", "Weight column says lbs, but these are kg", BASE + "#/shipments"),
    "FB-103": ("u-carla", "the shipments page is weird", BASE + "#/shipments"),
    "FB-104": ("u-anna", "Booking form says Pices instead of Pieces", BASE + "#/booking"),
    "FB-105": ("u-ben", "Invoice says Totla at the bottom", BASE + "#/invoices/INV-2291"),
}


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
        n_signals = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert len(labels) == 150
    assert Counter((l["category"], l["code_area"]) for l in labels)[("copy", "frontend")] == 40
    assert n_signals == 0


def test_live_ids_start_at_fb101(db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import api

    monkeypatch.setattr(api, "SCREENSHOT_DIR", tmp_path / "shots")
    client = TestClient(api.app)
    for sid, (user, text, url) in DEMO.items():
        r = client.post("/feedback", json={"type": "message", "channel": "C-FEEDBACK", "user": user, "text": text,
                                           "ts": str(time.time()), "files": [], "page_url": url, "client_ts": ""})
        assert r.status_code == 200 and r.json()["id"] == sid
        assert labels_call("find_duplicates", text=text, page_url=url) == [], sid


def test_no_duplicates_with_heatmap(db):
    from app import heatmap

    groups = heatmap.score_groups(heatmap.group_events(heatmap._windows(posthog.load_events(), posthog.now())[0]), {})
    for g in groups:
        sid = heatmap.signal_id_of(g)
        hm = {"text": heatmap.describe(g, sid), "route": g["route"], "element": g["element"]}
        for _, text, url in (DEMO["FB-104"], DEMO["FB-105"]):  # FB-101 matching the PDF group is intended
            me = {"text": text, "route": posthog.route_of(url), "element": ""}
            assert not is_duplicate(me, hm) and not is_duplicate(hm, me), (sid, text)


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

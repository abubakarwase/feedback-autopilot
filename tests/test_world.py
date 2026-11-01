"""Unit tests for the M0/M3 world: posthog, labels, slack/api, heatmap, evals, decide fixtures.

No network, no LLM: OFFLINE=1, DECIDE_BACKEND=mock, temp DB built with data/generate.py's write_db.
"""
import base64
import dataclasses
import importlib.util
import json
import random
import sqlite3
import time
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
FB = {
    "FB-101": ("u-anna", "Download button does nothing", BASE + "#/invoices/INV-2291"),
    "FB-102": ("u-ben", "Weight column says lbs, but these are kg", BASE + "#/shipments"),
    "FB-103": ("u-carla", "the shipments page is weird", BASE + "#/shipments"),
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


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


def add_feedback(path: Path, *ids: str) -> None:
    with sqlite3.connect(path) as con:
        for i, sid in enumerate(ids):
            user, text, url = FB[sid]
            con.execute("INSERT INTO signals (id, source, text, user, channel, file, page_url, route, element,"
                        " stats, ts, created_at) VALUES (?, 'feedback', ?, ?, 'C-FEEDBACK', '', ?, ?, '', '{}', ?, '')",
                        (sid, text, user, url, posthog.route_of(url), time.time() + i))


def sig(sid: str) -> dict:
    _, text, url = FB[sid]
    return {"text": text, "page_url": url}


# ---- posthog --------------------------------------------------------------

def test_route_of():
    assert posthog.route_of(BASE + "#/invoices/INV-2291") == "/invoices/:id"
    assert posthog.route_of(BASE + "?user=u-ben#/invoices/INV-2291") == "/invoices/:id"
    assert posthog.route_of("/invoices/INV-2291") == "/invoices/:id"
    assert posthog.route_of("/invoices/:id") == "/invoices/:id"
    assert posthog.route_of("#/booking") == "/booking"
    assert posthog.route_of("/shipments/") == "/shipments"
    assert posthog.route_of(BASE) == "/shipments"


def test_query_events_planted_counts():
    pdf = [e for e in posthog.query_events("$dead_click", BASE + "#/invoices/INV-2291")
           if posthog.selector_of(e["$elements_chain"])[0] == "button#download-pdf"]
    assert len(pdf) == 14 and len({e["distinct_id"] for e in pdf}) == 6
    assert "properties" not in pdf[0] and "$elements_chain" in pdf[0]
    assert len(posthog.query_events("$exception", "/invoices/:id")) == 14
    eta = [e for e in posthog.query_events("$rageclick", BASE + "#/shipments")
           if posthog.selector_of(e["$elements_chain"])[0] == "td.eta-cell span"]
    assert len(eta) == 41 and len({e["distinct_id"] for e in eta}) == 9
    # enrich reads events[0]: it must be the planted element
    first = posthog.query_events("$dead_click", BASE + "#/invoices/INV-2291")[0]
    assert "download-pdf" in first["$elements_chain"]


# ---- labels ---------------------------------------------------------------

def test_is_duplicate_demo_cases(db):
    from app import heatmap

    for sid in FB:
        assert labels_call("find_duplicates", text=FB[sid][1], page_url=FB[sid][2]) == []
        add_feedback(db, sid)
        assert labels_call("find_duplicates", text=FB[sid][1], page_url=FB[sid][2]) == []
    assert not is_duplicate(sig("FB-102"), sig("FB-103"))
    assert not is_duplicate(sig("FB-101"), sig("FB-101"))

    groups = {heatmap.signal_id_of(g): g for g in heatmap.score_groups(
        heatmap.group_events(heatmap._windows(posthog.load_events(), posthog.now())[0]), {})}
    hm = {sid: {"text": heatmap.describe(g, sid), "route": g["route"], "element": g["element"]}
          for sid, g in groups.items()}
    assert "Download PDF" in hm["HM-invoices-download-pdf"]["text"]
    assert is_duplicate(hm["HM-invoices-download-pdf"], {**sig("FB-101"), "route": "/invoices/:id"})
    dups = labels_call("find_duplicates", text=hm["HM-invoices-download-pdf"]["text"],
                       page_url=BASE + "#/invoices/INV-2291")
    assert [d["signal_id"] for d in dups] == ["FB-101"]
    eta = hm["HM-shipments-eta-cell"]
    assert not any(is_duplicate(eta, {**sig(s), "route": posthog.route_of(FB[s][2])}) for s in FB)
    assert labels_call("find_duplicates", text=eta["text"], page_url=BASE + "#/shipments") == []


def test_save_label_and_get_trust(db):
    r = labels_call("get_trust", category="bug", code_area="handler-frontend")
    assert r["trusted"] is False and r["cases"] == 0
    row = labels_call("save_label", signal_id="FB-101", category="bug", code_area="handler-frontend",
                      outcome="merged_unchanged", triage_corrections={"category": "ux"})
    assert row["signal_id"] == "FB-101" and row["triage_corrections"] == {"category": "ux"}
    with pytest.raises(Exception, match="unknown outcome"):
        labels_call("save_label", signal_id="FB-101", category="bug", code_area="x", outcome="nope")
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO trust VALUES ('copy', 'frontend', 40, 40, 0, 1, '')")
    assert labels_call("get_trust", category="copy", code_area="frontend")["trusted"] is True


def test_missing_table_raises(db):
    from app.tools import labels

    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE signals")
    with pytest.raises(RuntimeError, match="generate.py"):
        labels.db("signals")


# ---- heatmap --------------------------------------------------------------

def test_scan_order_and_no_double_insert(db):
    from app import heatmap

    new = heatmap.scan()
    assert [s["id"] for s in new] == ["HM-shipments-eta-cell", "HM-invoices-download-pdf"]
    assert new[0]["text"].startswith("41 rage clicks by 9 users") and "td.eta-cell span" in new[0]["text"]
    assert heatmap.scan() == []
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM signals WHERE source = 'heatmap'").fetchone()[0] == 2


def test_scan_skips_download_pdf_after_fb101(db):
    from app import heatmap

    add_feedback(db, "FB-101", "FB-102", "FB-103")
    assert [s["id"] for s in heatmap.scan()] == ["HM-shipments-eta-cell"]


def test_impact_check(db):
    from app import heatmap

    add_feedback(db, "FB-101", "FB-102")
    for sid, cat in (("FB-101", "bug"), ("FB-102", "copy")):
        labels_call("save_label", signal_id=sid, category=cat, code_area="frontend", outcome="merged_unchanged")
    res = {r["signal_id"]: r for r in heatmap.impact_check()}
    assert res["FB-101"]["verdict"] == "confirmed"
    assert (res["FB-101"]["before"], res["FB-101"]["after"]) == (14, 0)
    assert res["FB-101"]["message"] == "FB-101 fix confirmed: dead clicks 14 -> 0"
    assert res["FB-102"]["verdict"] == "skipped"
    heatmap.impact_check()
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT signal_id, verdict FROM impact_checks ORDER BY signal_id").fetchall()
    assert rows == [("FB-101", "confirmed"), ("FB-102", "skipped")]


# ---- evals ----------------------------------------------------------------

def test_evals_trust(db):
    from evals.run import run

    r = run()
    assert r["scores"]["trusted"] == ["copy · frontend"]
    assert r["scores"]["combos"]["copy · frontend"]["cases"] == 40
    assert labels_call("get_trust", category="copy", code_area="frontend")["trusted"] is True
    h = labels_call("get_trust", category="bug", code_area="handler-frontend")
    assert h["cases"] == 12 and h["trusted"] is False
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 1


# ---- api + slack ----------------------------------------------------------

def test_post_feedback(db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import api
    from app.tools import slack

    monkeypatch.setattr(api, "SCREENSHOT_DIR", tmp_path / "shots")
    client = TestClient(api.app)

    def body(sid, files):
        user, text, url = FB[sid]
        return {"type": "message", "channel": "C-FEEDBACK", "user": user, "text": text, "ts": str(time.time()),
                "files": files, "page_url": url, "client_ts": ""}

    shot = [{"name": "screenshot.png", "mimetype": "image/png",
             "data_url": "data:image/png;base64," + base64.b64encode(PNG).decode()}]
    r1 = client.post("/feedback", json=body("FB-101", shot))
    assert r1.status_code == 200 and r1.json()["id"] == "FB-101"
    r2 = client.post("/feedback", json=body("FB-102", []))
    assert r2.json()["id"] == "FB-102"
    assert client.post("/feedback", json={**body("FB-103", []), "type": "reaction"}).status_code == 422

    fb = slack.get_feedback("FB-101")
    assert (fb["user"], fb["route"], fb["source"]) == ("u-anna", "/invoices/:id", "feedback")
    assert Path(fb["file"]).read_bytes() == PNG
    assert slack.get_feedback("FB-102")["file"] == ""
    # generate.py also seeds older history signals; the two just posted come first, newest first
    assert [s["id"] for s in slack.list_signals()][:2] == ["FB-102", "FB-101"]
    assert slack.post_reply("FB-101", "Fixed.")["ok"] is True
    assert [r["text"] for r in slack.list_replies("FB-101")] == ["Fixed."]
    with pytest.raises(KeyError):
        slack.get_feedback("FB-999")


# ---- decide fixtures ------------------------------------------------------

def test_decide_fixtures_match_graph():
    fx = json.loads((ROOT / "data" / "decide_fixtures.json").read_text())
    options = {graph.Q_CATEGORY: graph.CATEGORIES, graph.Q_AREA: graph.AREAS, graph.Q_FIXABLE: ["yes", "no"]}
    for sid, qs in fx.items():
        if sid.startswith("_"):
            continue
        assert set(qs) == set(options), sid
        for q, tries in qs.items():
            assert all(t["answer"] in options[q] for t in tries), (sid, q)


def test_fb103_low_p_twice_then_human(db):
    add_feedback(db, "FB-103")
    ev = list(graph.run_signal("FB-103"))
    assert [e.p for e in ev if e.node == "triage"] == [0.48, 0.55]
    assert ev[-1].node == "human_triage"

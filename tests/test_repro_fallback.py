"""reproduce(): fall back to the hand-written repro test when the AI test does not fail on the buggy code."""
import dataclasses
from pathlib import Path

import pytest

from app import config, graph

WEAK = "def test_weak():\n    assert True\n"
RED = "def test_red():\n    assert False\n"
BROKEN = "def test_broken(no_such_fixture):\n    pass\n"


def _state(sid, wt):
    return {"signal_id": sid, "worktree": str(wt), "retries": {},
            "signal": {"text": "Download PDF does nothing", "page_url": "/#/invoices/INV-2291"},
            "evidence": {"element_selector": "#download-pdf", "console_errors": ["TypeError: x"], "network_errors": []},
            "issue": {"page": "/#/invoices/INV-2291", "element": "#download-pdf", "expected": "PDF downloads",
                      "actual": "nothing", "candidate_files": []}}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, OFFLINE=False, MAX_RETRIES=2))
    runs = []

    def fake_run_tests(wt, rel):
        code = (Path(wt) / rel).read_text()
        runs.append(code)
        rc = 0 if code in (WEAK,) else 1  # weak AI test passes; anything else (RED, BROKEN, hand-written) fails
        summary = "1 passed" if rc == 0 else ("1 error in 0.01s" if code == BROKEN else "1 failed")
        return {"passed": rc == 0, "returncode": rc, "summary": summary, "output": ""}

    monkeypatch.setattr(graph.repo, "run_tests", fake_run_tests)
    return tmp_path, runs


def _llm(code, prompts):
    def f(task, schema, prompt, images=None):
        prompts.append(prompt)
        return schema(filename="test_x.py", code=code)
    return f


def test_weak_ai_test_falls_back_to_handwritten(setup, monkeypatch):
    wt, runs = setup
    prompts = []
    monkeypatch.setattr(graph, "llm", _llm(WEAK, prompts))
    out = graph.reproduce(_state("FB-101", wt))
    trace = out["trace"][0]["output"]
    assert out["fix"]["test_failed_before"] is True and out["route"] == "fix"
    assert trace["fallback"] == "ai_test_did_not_fail_on_buggy_code -> used hand-written repro test"
    assert trace["ai_test_summary"] == "1 passed"
    handwritten = Path("data/repro_tests/test_fb101_download_pdf.py").read_text()
    assert (wt / out["fix"]["test_path"]).read_text() == handwritten
    assert runs == [WEAK, handwritten]
    assert "#download-pdf" in prompts[0] and "TypeError: x" in prompts[0] and "Download PDF does nothing" in prompts[0]


def test_weak_ai_test_without_handwritten_goes_back_to_enrich(setup, monkeypatch):
    wt, runs = setup
    monkeypatch.setattr(graph, "llm", _llm(WEAK, []))
    out = graph.reproduce(_state("FB-999", wt))
    assert out["fix"]["test_failed_before"] is False and out["route"] == "enrich"
    assert "fallback" not in out["trace"][0]["output"]
    assert runs == [WEAK]


def test_red_ai_test_is_kept(setup, monkeypatch):
    wt, runs = setup
    monkeypatch.setattr(graph, "llm", _llm(RED, []))
    out = graph.reproduce(_state("FB-101", wt))
    assert out["fix"]["test_failed_before"] is True and out["route"] == "fix"
    assert "fallback" not in out["trace"][0]["output"]
    assert (wt / out["fix"]["test_path"]).read_text() == RED


def test_erroring_ai_test_falls_back_to_handwritten(setup, monkeypatch):
    wt, runs = setup
    monkeypatch.setattr(graph, "llm", _llm(BROKEN, []))
    out = graph.reproduce(_state("FB-101", wt))
    trace = out["trace"][0]["output"]
    assert out["fix"]["test_failed_before"] is True and out["route"] == "fix"
    assert trace["ai_test_summary"] == "1 error in 0.01s"
    assert runs[0] == BROKEN and len(runs) == 2

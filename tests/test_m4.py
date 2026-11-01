import json

from langgraph.checkpoint.memory import MemorySaver

from app import cards, graph
from app.llm import _placeholder
from app.schemas import ScreenCard

VIEW = dict(what_happens="w", how_people_use_it="h", where_it_breaks="b", what_changed="c")
CARD = ScreenCard(screen="invoice", route="/invoice", forwarder=VIEW, developer=VIEW)


def test_get_card(tmp_path, monkeypatch):
    f = tmp_path / "cards.json"
    monkeypatch.setattr(cards, "CARDS_PATH", f)
    assert cards.get_card("/invoice") is None  # no file
    f.write_text(json.dumps([CARD.model_dump()]))
    assert cards.get_card("/invoice").screen == "invoice"
    assert cards.get_card("/invoice/42").screen == "invoice"
    assert cards.get_card("/shipments") is None


def run(monkeypatch, card):
    replies, issues = [], []
    monkeypatch.setattr(graph.slack, "get_feedback", lambda sid: dict(
        text="how do I add a fee?", user="u", channel="c", page_url="/invoice", ts="1"))
    monkeypatch.setattr(graph.posthog, "query_events", lambda *a, **k: [])
    monkeypatch.setattr(graph.posthog, "get_heatmap", lambda u: {})
    monkeypatch.setattr(graph, "llm", lambda task, schema, prompt, images=None: (
        graph.HowToAnswer(answer="Use Add fee.", card_section="words", sources=["x"])
        if task == "how_to_answer" else _placeholder(schema)))
    monkeypatch.setattr(graph, "decide", lambda q, opts, ctx, sid, n: {
        "answer": "how_to" if opts[0] == "bug" else opts[-1] if opts[0] == "yes" else opts[0], "p": 0.95})
    monkeypatch.setattr(graph, "labels_call", lambda name, **k: [] if name == "find_duplicates" else {})
    monkeypatch.setattr(graph, "get_card", lambda url: card)
    monkeypatch.setattr(graph.slack, "post_reply", lambda sid, t: replies.append(t) or {})
    monkeypatch.setattr(graph.github, "create_issue", lambda t, b: issues.append(b) or {})
    g = graph.build(MemorySaver())
    nodes = [n for c in g.stream({"signal_id": "FB-X", "retries": {}, "trace": []},
                                 {"configurable": {"thread_id": "t"}}, stream_mode="updates") for n in c]
    return nodes, replies, issues




def test_how_to_with_card(monkeypatch):
    nodes, replies, issues = run(monkeypatch, CARD)
    assert nodes[-1] == "answer_how_to" and replies == ["Use Add fee."] and not issues


def test_how_to_without_card(monkeypatch):
    nodes, replies, issues = run(monkeypatch, None)
    assert nodes[-2:] == ["answer_how_to", "open_issue"] and not replies and "no screen card" in issues[0]


def _cards(tmp_path, monkeypatch, text):
    f = tmp_path / "c.json"
    f.write_text(text if isinstance(text, str) else json.dumps(text))
    monkeypatch.setattr(cards, "CARDS_PATH", f)


def test_truncated_json(tmp_path, monkeypatch):
    _cards(tmp_path, monkeypatch, '[{"screen": "inv')
    assert cards.get_card("/invoice") is None


def test_invalid_card_skipped(tmp_path, monkeypatch):
    _cards(tmp_path, monkeypatch, [{"screen": "x", "route": "/invoice"}, CARD.model_dump()])
    assert cards.get_card("/invoice").screen == "invoice"


def test_root_card_does_not_win(tmp_path, monkeypatch):
    root = {**CARD.model_dump(), "screen": "home", "route": "/"}
    inv = {**CARD.model_dump(), "screen": "invoices", "route": "/invoices"}
    _cards(tmp_path, monkeypatch, [root, inv])
    assert cards.get_card("/invoices").screen == "invoices"
    assert cards.get_card("/other") is None


def test_param_route(tmp_path, monkeypatch):
    _cards(tmp_path, monkeypatch, [{**CARD.model_dump(), "screen": "zzz", "route": "/invoices/:id"}])
    assert cards.get_card("/invoices/42") is not None
    assert cards.get_card("/invoicesX/42") is None

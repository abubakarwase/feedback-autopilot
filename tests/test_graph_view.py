from app.graph_view import core_dot, core_edges, trace_dot


def ev(node, kind="python", input=None, output=None, **kw):
    return {"node": node, "kind": kind, "input": input, "output": output, "p": None, "retry": 0, **kw}


def test_core_edges():
    _, edges = core_edges()
    pairs = {(a, b) for a, b, _ in edges}
    assert ("triage", "enrich") in pairs
    assert ("merge_gate", "developer_review") in pairs


def test_core_dot_states():
    events = [ev("collect_signal"), ev("enrich"), ev("enrich"), ev("developer_review", "human")]
    d = core_dot(events)
    line = lambda n: next(x for x in d.splitlines() if x.startswith(f'"{n}" ['))
    assert "#D5E4C8" in line("collect_signal")
    assert "×2" in line("enrich")
    assert "#F6D77A" in line("developer_review")
    assert "#EDEBE5" in line("verify")


def test_trace_dot_tooltips_and_escape():
    events = [ev("a", input={"q": 'he said "hi"\nbye'}, output={"x": 1}), ev("b", "llm", p=0.94)]
    d = trace_dot(events)
    assert d.count("tooltip=") == 2
    assert "INPUT:" in d and "OUTPUT:" in d
    assert "hi" in d and "bye" in d
    assert all(l.startswith(("s", "d", "r", "n", "}")) for l in d.splitlines())
    assert "p 0.94" in d

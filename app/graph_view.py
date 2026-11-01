"""DOT strings for the live agent graph (st.graphviz_chart). Pure functions."""
import json
from functools import lru_cache

BORDER = {"tool": "#6A9BCC", "llm": "#D97757", "python": "#788C5D", "decision": "#8A8880",
          "human": "#D97757", "graph": "#8A8880"}
GREY, GREEN, ORANGE, AMBER, ACCENT = "#EDEBE5", "#D5E4C8", "#F3B58F", "#F6D77A", "#D97757"


@lru_cache
def core_edges() -> tuple:
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph import build

    g = build(MemorySaver()).get_graph()
    nodes = tuple(n for n in g.nodes if not n.startswith("__"))
    edges = tuple((e.source, e.target, e.data if e.conditional and isinstance(e.data, str) else None)
                  for e in g.edges if not e.source.startswith("__") and not e.target.startswith("__"))
    return nodes, edges


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")


def _short(obj, n: int = 1200) -> str:
    return esc(json.dumps(obj, default=str, indent=1)[:n])


def _tip(e: dict) -> str:
    return f"INPUT:\\n{_short(e.get('input'))}\\n\\nOUTPUT:\\n{_short(e.get('output'))}"


def _paused(e: dict) -> bool:
    return e["kind"] == "human" and e.get("input") is None


def core_dot(events: list[dict]) -> str:
    nodes, edges = core_edges()
    last: dict[str, dict] = {}
    count: dict[str, int] = {}
    for e in events:
        last[e["node"]] = e
        count[e["node"]] = count.get(e["node"], 0) + 1
    cur = events[-1]["node"] if events else None
    hit = {(a["node"], b["node"]) for a, b in zip(events, events[1:])}
    out = ['digraph G {', 'rankdir=TB; nodesep=0.25; ranksep=0.3;',
           'node [shape=box style="rounded,filled" fontsize=11 fontname="Helvetica"];',
           'edge [fontsize=9 color="#B0AEA5"];']
    for n in nodes:
        e = last.get(n)
        fill, border, tip, label = GREY, "#B0AEA5", n, n
        if e:
            border = BORDER.get(e["kind"], "#8A8880")
            fill = AMBER if _paused(e) else ORANGE if n == cur else GREEN
            if e["kind"] == "human" and not _paused(e):
                fill = GREEN
            tip = _tip(e)
            if count[n] > 1:
                label = f"{n}\\n×{count[n]}"
        out.append(f'"{n}" [label="{label}" fillcolor="{fill}" color="{border}" tooltip="{tip}"];')
    for a, b, lab in edges:
        attrs = f'label="{esc(lab)}" ' if lab else ""
        if (a, b) in hit:
            attrs += f'penwidth=2.5 color="{ACCENT}"'
        out.append(f'"{a}" -> "{b}" [{attrs}];')
    out.append("}")
    return "\n".join(out)


def trace_dot(events: list[dict]) -> str:
    out = ['digraph T {', 'rankdir=LR; nodesep=0.25; ranksep=0.3;',
           'node [shape=box style="rounded,filled" fontsize=11 fontname="Helvetica"];']
    for i, e in enumerate(events):
        fill = AMBER if _paused(e) else ORANGE if i == len(events) - 1 else GREEN
        p = f" · p {e['p']:.2f}" if e.get("p") is not None else ""
        label = f"{esc(e['node'])}\\n{esc(e['kind'])}{p} · r{e.get('retry', 0)}"
        out.append(f's{i} [label="{label}" fillcolor="{fill}" '
                   f'color="{BORDER.get(e["kind"], "#8A8880")}" tooltip="{_tip(e)}"];')
        if i:
            out.append(f's{i-1} -> s{i};')
    out.append("}")
    return "\n".join(out)

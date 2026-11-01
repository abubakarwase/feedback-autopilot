"""Nightly trigger (what cron calls): heatmap scan -> graph per new signal -> impact check -> evals -> shared map.

run() yields progress dicts {"step", "message", "data"} so the UI can stream them.
"""
import importlib
from typing import Iterator


def _load(module: str):
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        raise RuntimeError(f"nightly needs {module}, which could not be imported: {e}") from e


def _item(step: str, message: str, data=None) -> dict:
    return {"step": step, "message": message, "data": data}


def run() -> Iterator[dict]:
    heatmap = _load("app.heatmap")
    evals_run = _load("evals.run")
    map_builder = _load("app.map_builder")
    graph = _load("app.graph")

    signals = heatmap.scan()
    ids = [s["id"] for s in signals]
    yield _item("heatmap_scan", f"{len(ids)} new heatmap signal(s): {', '.join(ids) or 'none'}", signals)

    for sid in ids:
        last = None
        for ev in graph.run_signal(sid):
            last = ev
            yield {"step": "graph", "signal_id": sid, "message": f"{sid} {ev.node} ({ev.kind})",
                   "event": ev.model_dump(), "data": None}
        if last is not None and last.kind == "human":
            # Paused for a developer; the UI resumes it. Move on to the next signal.
            yield _item("graph_paused", f"{sid} waits for a developer at {last.node}",
                        {"signal_id": sid, "node": last.node})

    checks = heatmap.impact_check()
    for c in checks:
        yield _item("impact_check", c["message"], c)
    if not checks:
        yield _item("impact_check", "no merged fixes to check", [])

    result = evals_run.run()
    sc = result["scores"]
    yield _item("evals", f"evals: {sc['cases']} cases, triage accuracy {sc['triage_accuracy']:.0%}, "
                         f"trusted: {', '.join(sc['trusted']) or 'none'}", result)

    cards = map_builder.build_all()
    yield _item("map_builder", f"{len(cards)} screen card(s): {', '.join(c.screen for c in cards)}",
                [c.model_dump() for c in cards])


if __name__ == "__main__":
    for item in run():
        print(f"[{item['step']}] {item['message']}", flush=True)

"""Every graph decision goes through decide(). Backend: DECIDE_BACKEND = mock | llm | jev."""
import json
from pathlib import Path

from pydantic import BaseModel

from app import config
from app.llm import llm

FIXTURES = Path("data/decide_fixtures.json")


class _Answer(BaseModel):
    answer: str
    p: float


def _from_fixtures(question: str, signal_id: str | None, try_n: int) -> dict | None:
    if not signal_id or not FIXTURES.exists():
        return None
    tries = json.loads(FIXTURES.read_text()).get(signal_id, {}).get(question)
    if not tries:
        return None
    e = tries[try_n - 1] if try_n - 1 < len(tries) else tries[-1]
    return {"answer": e["answer"], "p": float(e["p"]), "backend": "mock"}


def _ask_llm(question: str, options: list[str], context: dict) -> dict:
    if config.settings.OFFLINE:
        return {"answer": options[0], "p": 0.9, "backend": "offline"}
    r = llm("decide", _Answer,
            f"Question: {question}\nOptions: {options}\nContext: {json.dumps(context, default=str)}\n"
            "Answer with exactly one option and your confidence p in 0..1.")
    ans = r.answer if r.answer in options else options[0]
    return {"answer": ans, "p": r.p, "backend": "llm"}


def decide(question: str, options: list[str], context: dict,
           signal_id: str | None = None, try_n: int = 1) -> dict:
    backend = config.settings.DECIDE_BACKEND
    if backend == "jev":
        raise NotImplementedError("jev backend")
    if backend == "mock":
        hit = _from_fixtures(question, signal_id, try_n)
        if hit:
            return hit
    return _ask_llm(question, options, context)

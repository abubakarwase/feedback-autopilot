"""Screen Card lookup. Reads data/screen_cards.json (written by map_builder): a JSON list of ScreenCard dicts."""
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from app import config
from app.schemas import ScreenCard

CARDS_PATH = Path(__file__).resolve().parent.parent / "data" / "screen_cards.json"


def _validate(raw) -> list[ScreenCard]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        try:
            out.append(ScreenCard.model_validate(c))
        except ValidationError:
            pass
    return out


def _load_db() -> list[ScreenCard]:
    try:
        con = sqlite3.connect(f"file:{config.settings.DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT card FROM screen_cards").fetchall()
        finally:
            con.close()
        return _validate([json.loads(r[0]) for r in rows])
    except (sqlite3.Error, ValueError, TypeError):
        return []


def _load() -> list[ScreenCard]:
    try:
        out = _validate(json.loads(CARDS_PATH.read_text()))
    except (OSError, ValueError):
        out = []
    return out or _load_db()


def _segs(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _route_match(route: str, url_segs: list[str]) -> bool:
    rs = _segs(route)
    return bool(rs) and len(rs) <= len(url_segs) and all(
        r.startswith(":") or r == u for r, u in zip(rs, url_segs))


def get_card(page_url: str) -> ScreenCard | None:
    url = page_url or ""
    u = urlparse(url)
    url_segs = _segs(u.fragment.split("?")[0] if u.fragment else u.path or url)
    cards = _load()
    hits = [c for c in cards if _route_match(c.route, url_segs)]
    if hits:
        return max(hits, key=lambda c: len(_segs(c.route)))
    named = [c for c in cards if len(c.screen) >= 4 and c.screen.lower() in url.lower()]
    return max(named, key=lambda c: len(c.screen)) if named else None

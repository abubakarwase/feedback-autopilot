"""Shared map tab: one Screen Card per screen, the same facts in forwarder words and developer words."""
import re
import sqlite3
from pathlib import Path

import streamlit as st

from app import config
from app.map_builder import SCREENS
from app.schemas import ScreenCard

NAMES = {"shipments": "Shipments list", "invoice": "Invoice", "booking": "Booking"}
VIEWS = ["Side by side", "Forwarder", "Developer"]
# STATUS TAG CONTRACT with map_builder: bullets "- [tag] text"
TAG_COLORS = {"open": "red", "in review": "orange", "triaged": "orange", "to product": "blue",
              "fixed · auto": "green", "fixed · reviewed": "green", "confirmed": "green",
              "rejected": "gray", "duplicate": "gray"}
TAG_RE = re.compile(r"^\[(" + "|".join(map(re.escape, TAG_COLORS)) + r")\]\s*(.*)$")


def _rows(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    path = config.settings.DB_PATH
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} not found; run: uv run python data/generate.py")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"{path}: {e}; run: uv run python data/generate.py") from e
    finally:
        con.close()


def _status(card: ScreenCard) -> str:
    """'N broken · M fixed' counted from the card's own tags (developer view keeps the $exception lines)."""
    tags = lambda text: [m.group(1) for m in map(TAG_RE.match, _items(text)) if m]
    broken = len(tags(card.developer.where_it_breaks))
    fixed = sum(t.startswith("fixed") for t in tags(card.developer.what_changed))
    return f"{broken} broken · {fixed} fixed"


def _esc(text: str) -> str:
    # "$a ... $b" would render as LaTeX and "BKG-*" as emphasis
    return text.replace("$", r"\$").replace("*", r"\*")


def _items(text: str) -> list[str]:
    """Bullet strings ('- a\\n- b') as items; otherwise split into sentences outside quotes."""
    lines = [l.strip()[2:] for l in text.splitlines() if l.strip().startswith("- ")]
    if lines:
        return lines
    out, start, quoted = [], 0, False
    for m in re.finditer(r'"|(?<=[.!?])\s+(?=[A-Z0-9$#"])', text):
        if m.group() == '"':
            quoted = not quoted
        elif not quoted:
            out.append(text[start:m.start()])
            start = m.end()
    out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def _text(text: str) -> None:
    items = _items(text)
    if len(items) == 1:
        st.markdown(_esc(items[0]))
    else:
        st.markdown("\n".join(f"- {_esc(i)}" for i in items))


def _tagged(text: str) -> None:
    """Bullets with a status tag render as a coloured badge + text; the rest as plain items."""
    lines = []
    for item in _items(text):
        m = TAG_RE.match(item)
        lines.append(f":{TAG_COLORS[m.group(1)]}-badge[{m.group(1)}] {_esc(m.group(2))}" if m else _esc(item))
    if not lines:
        st.caption("Nothing recorded.")
    else:
        st.markdown("\n".join(f"- {l}" for l in lines) if len(lines) > 1 else lines[0])


def _show(attr: str, card: ScreenCard, view: str, show) -> None:
    if view == "Side by side":
        left, right = st.columns(2, border=True)
        with left:
            st.caption("Forwarder words")
            show(getattr(card.forwarder, attr))
        with right:
            st.caption("Developer words")
            show(getattr(card.developer, attr))
    else:
        show(getattr(card.forwarder if view == "Forwarder" else card.developer, attr))


def _words(card: ScreenCard) -> None:
    seen, rows = set(), []
    for w in card.forwarder.words + card.developer.words:
        if (w.forwarder_term, w.code_name) not in seen:
            seen.add((w.forwarder_term, w.code_name))
            rows.append({"forwarder says": w.forwarder_term, "code calls it": w.code_name})
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("No word pairs for this screen.")


def _pick(screen: str) -> None:
    st.session_state["map_screen"] = screen


def render_map_tab() -> None:
    top, rebuild = st.columns([5, 1])
    top.markdown("One card per screen, written nightly from usage, feedback and fixes. "
                 "**Same facts, two languages.**")
    if rebuild.button("Rebuild map", width="stretch"):
        from app import map_builder

        with st.spinner("Building screen cards…"):
            map_builder.build_all()
        st.rerun()

    rows = {r["screen"]: r for r in _rows("SELECT * FROM screen_cards")}
    if not rows:
        st.info("No screen cards yet. Run nightly or press Rebuild map.")
        return
    if st.session_state.get("map_screen") not in rows:
        st.session_state["map_screen"] = "invoice" if "invoice" in rows else next(s for s in SCREENS if s in rows)
    selected = st.session_state["map_screen"]

    for col, (screen, route) in zip(st.columns(len(SCREENS)), SCREENS.items()):
        with col.container(border=True):
            st.markdown(f"**{NAMES[screen]}**")
            st.caption(f"`{route}`")
            st.markdown(_status(ScreenCard.model_validate_json(rows[screen]["card"])) if screen in rows
                        else "No card yet")
            st.button("Viewing" if screen == selected else "Open card", key=f"map_pick_{screen}",
                      type="primary" if screen == selected else "secondary", width="stretch",
                      disabled=screen not in rows, on_click=_pick, args=(screen,))

    row = rows[selected]
    card = ScreenCard.model_validate_json(row["card"])
    st.subheader(f"{NAMES[selected]} · `{card.route}`")
    view = st.segmented_control("View", VIEWS, default=VIEWS[0], key="map_view", label_visibility="collapsed")
    if view is None:
        st.info("Pick a view: Side by side, Forwarder or Developer.")
        return

    st.markdown("### Where it breaks")
    _show("where_it_breaks", card, view, _tagged)
    st.markdown("### What changed and why")
    _show("what_changed", card, view, _tagged)
    st.caption("Fixed automatically = this kind of fix has a clean record · "
               "Reviewed = a developer approved it · To product = not a code bug")

    with st.expander("About this screen"):
        st.markdown("#### What this screen is for")
        _show("what_happens", card, view, _text)
        st.markdown("#### How people use it")
        _show("how_people_use_it", card, view, _text)
        st.markdown("#### Words")
        _words(card)

    with st.expander(f"Where these facts come from ({len(card.sources)} sources)"):
        st.markdown("\n".join(f"- **{s.kind}** · {_esc(s.ref)}" for s in card.sources) or "No sources.")
    st.caption(f"Card version {row['version']} · last updated {row['updated_at']}")

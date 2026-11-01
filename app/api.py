"""Feedback intake (stands in for the Slack event) + serves the target app at /app."""
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config
from app.tools import posthog, slack

SCREENSHOT_DIR = Path("data/screenshots")
PNG_PREFIX = "data:image/png;base64,"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SlackFile(BaseModel):
    name: str
    mimetype: str
    data_url: str


class FeedbackEvent(BaseModel):
    type: str
    channel: str
    user: str = Field(min_length=1)
    text: str = Field(min_length=1)
    ts: str
    files: list[SlackFile] = []
    page_url: str = Field(min_length=1)
    client_ts: str = ""


app = FastAPI(title="Feedback Autopilot intake")


@app.middleware("http")
async def no_store_app(request, call_next):
    # after a merge the reload must fetch the fixed app.js, not a heuristically cached copy
    response = await call_next(request)
    if request.url.path.startswith("/app"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _decode_png(f: SlackFile) -> bytes:
    if f.mimetype != "image/png" or not f.data_url.startswith(PNG_PREFIX):
        raise HTTPException(422, f"file {f.name!r} is not a PNG data URL")
    try:
        data = base64.b64decode(f.data_url[len(PNG_PREFIX):], validate=True)
    except ValueError as e:
        raise HTTPException(422, f"file {f.name!r}: bad base64: {e}") from e
    if not data.startswith(PNG_MAGIC):
        raise HTTPException(422, f"file {f.name!r}: not PNG data")
    return data


@app.post("/feedback")
def post_feedback(ev: FeedbackEvent) -> dict:
    if ev.type != "message":
        raise HTTPException(422, f"type must be 'message', got {ev.type!r}")
    try:
        ts = float(ev.ts)
    except ValueError as e:
        raise HTTPException(422, f"ts must be a Slack timestamp, got {ev.ts!r}") from e
    if len(ev.files) > 1:
        raise HTTPException(422, "at most one screenshot file")
    png = _decode_png(ev.files[0]) if ev.files else None
    route = posthog.route_of(ev.page_url)

    with slack._conn() as con:
        # Live posts count from FB-101 (ids below 101 are never assigned here).
        n = con.execute("SELECT COUNT(*) FROM signals WHERE source = 'feedback' "
                        "AND CAST(substr(id, 4) AS INTEGER) >= 101").fetchone()[0]
        sid = f"FB-{101 + n}"
        file = ""
        if png is not None:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SCREENSHOT_DIR / f"{sid}.png"
            path.write_bytes(png)
            file = str(path)
        con.execute(
            "INSERT INTO signals (id, source, text, user, channel, file, page_url, route, element,"
            " stats, ts, created_at) VALUES (?, 'feedback', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            (sid, ev.text, ev.user, ev.channel, file, ev.page_url, route, json.dumps({}), ts,
             datetime.now(timezone.utc).isoformat()))
    return {"ok": True, "id": sid}


@app.get("/signals")
def get_signals() -> list[dict]:
    return slack.list_signals()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": config.settings.DB_PATH, "target_app": TARGET_APP}


TARGET_APP = ".worktrees/demo/target-app" if Path(".worktrees/demo/target-app").is_dir() else "target-app"
print(f"[api] serving target app from {TARGET_APP} at /app")
app.mount("/app", StaticFiles(directory=TARGET_APP, html=True), name="app")

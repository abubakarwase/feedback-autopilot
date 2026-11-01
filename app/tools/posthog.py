"""PostHog tools (mocked: data/posthog_events.json)."""


def get_session_events(session_id: str) -> list[dict]:
    """Return all events of one session."""
    # STUB: replaced by Nils
    return [
        {"event": "$dead_click", "distinct_id": "u1", "$session_id": session_id,
         "$current_url": "/invoices/:id", "$elements_chain": "button#download-pdf",
         "timestamp": "2026-10-30T10:00:00Z"},
        {"event": "$exception", "distinct_id": "u1", "$session_id": session_id,
         "$current_url": "/invoices/:id", "message": "TypeError: Cannot read properties of null",
         "timestamp": "2026-10-30T10:00:01Z"},
    ]


def query_events(event: str, url: str | None = None, hours: int = 24) -> list[dict]:
    """Return events of one type, optionally for one page, from the last `hours`."""
    # STUB: replaced by Nils
    return [{"event": event, "distinct_id": "u1", "$session_id": "s1",
             "$current_url": url or "/invoices/:id", "$elements_chain": "button#download-pdf",
             "timestamp": "2026-10-30T10:00:00Z"}]


def get_heatmap(url: str) -> dict:
    """Return click-group stats for a page."""
    # STUB: replaced by Nils
    return {"url": url, "groups": [
        {"selector": "button#download-pdf", "users": 6, "dead_clicks": 14, "rage_clicks": 0}]}

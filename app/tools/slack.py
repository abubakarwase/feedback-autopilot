"""Slack tools (mocked: feedback lives in SQLite)."""


def get_feedback(signal_id: str) -> dict:
    """Return the stored Slack feedback message for a signal id."""
    # STUB: replaced by Nils
    return {
        "id": "FB-101",
        "text": "Download button does nothing",
        "user": "U_forwarder_1",
        "channel": "#feedback",
        "file": "screenshots/FB-101.png",
        "page_url": "/invoices/:id",
        "ts": 1793523600.0,
    }


def post_reply(signal_id: str, text: str) -> dict:
    """Post a reply to the feedback thread."""
    # STUB: replaced by Nils
    return {"ok": True, "signal_id": signal_id, "text": text}

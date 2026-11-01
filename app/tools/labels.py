"""Label store tools (SQLite). Reach them from graph code only via app.mcp_client."""


def find_duplicates(text: str, page_url: str = "") -> list[dict]:
    """Return open issues/signals similar to `text`."""
    # STUB: replaced by Nils
    return []


def get_similar_cases(category: str, code_area: str, limit: int = 5) -> list[dict]:
    """Return past labelled decisions for a category and code area."""
    # STUB: replaced by Nils
    return [{"signal_id": "FB-050", "category": category, "code_area": code_area,
             "outcome": "merged_unchanged"}]


def get_trust(category: str, code_area: str) -> dict:
    """Return the trust stats for category + code area."""
    # STUB: replaced by Nils
    return {"category": category, "code_area": code_area, "cases": 12,
            "merged_unchanged": 12, "reverts": 0, "trusted": False}


def save_label(signal_id: str, category: str, code_area: str, outcome: str,
               triage_corrections: dict | None = None) -> dict:
    """Store a label for a decided case."""
    # STUB: replaced by Nils
    return {"ok": True, "signal_id": signal_id, "outcome": outcome}

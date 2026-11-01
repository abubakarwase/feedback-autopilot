"""FastMCP server exposing the label tools."""
from fastmcp import FastMCP

from app.tools import labels

mcp = FastMCP("labels")


@mcp.tool
def find_duplicates(text: str, page_url: str = "") -> list[dict]:
    """Return open issues/signals similar to `text`."""
    return labels.find_duplicates(text, page_url)


@mcp.tool
def get_similar_cases(category: str, code_area: str, limit: int = 5) -> list[dict]:
    """Return past labelled decisions for a category and code area."""
    return labels.get_similar_cases(category, code_area, limit)


@mcp.tool
def get_trust(category: str, code_area: str) -> dict:
    """Return the trust stats for category + code area."""
    return labels.get_trust(category, code_area)


@mcp.tool
def save_label(signal_id: str, category: str, code_area: str, outcome: str,
               triage_corrections: dict | None = None) -> dict:
    """Store a label for a decided case."""
    return labels.save_label(signal_id, category, code_area, outcome, triage_corrections)


if __name__ == "__main__":
    mcp.run()

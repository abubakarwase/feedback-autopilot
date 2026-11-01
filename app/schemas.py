"""Shared Pydantic models. Change only in a PR of its own."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class Signal(BaseModel):
    id: str
    source: Literal["feedback", "heatmap"] = "feedback"
    text: str = ""
    user: str = ""
    channel: str = ""
    screenshot: str | None = None  # file path / url
    page_url: str = ""
    ts: float = 0.0


class Evidence(BaseModel):
    session_events: list[dict[str, Any]] = Field(default_factory=list)
    rage_clicks: int = 0
    dead_clicks: int = 0
    console_errors: list[str] = Field(default_factory=list)
    network_errors: list[str] = Field(default_factory=list)
    element_selector: str = ""
    heatmap_stats: dict[str, Any] = Field(default_factory=dict)


class Issue(BaseModel):
    page: str
    element: str
    expected: str
    actual: str
    steps_to_reproduce: list[str] = Field(default_factory=list)
    candidate_files: list[str] = Field(default_factory=list)


Category = Literal["bug", "copy", "ux", "feature_request", "how_to"]


class Triage(BaseModel):
    duplicate_of: str | None = None
    category: Category
    category_p: float
    code_area: str
    code_area_p: float
    severity: Literal["low", "medium", "high"] = "medium"
    fixable: bool
    fixable_p: float

    @property
    def p(self) -> float:
        return min(self.category_p, self.code_area_p, self.fixable_p)


class FixAttempt(BaseModel):
    branch: str
    test_path: str = ""
    test_failed_before: bool = False
    tests_pass_after: bool = False
    lint_ok: bool = False
    diff_lines: int = 0
    files: list[str] = Field(default_factory=list)
    touched_protected_paths: list[str] = Field(default_factory=list)
    p: float = 0.0


class PullRequest(BaseModel):
    number: int
    url: str
    branch: str
    state: Literal["needs_review", "merged", "rejected"] = "needs_review"
    gate_result: dict[str, Any] = Field(default_factory=dict)
    merged_by: Literal["auto", "developer"] | None = None


class Label(BaseModel):
    signal_id: str
    category: Category
    code_area: str
    outcome: Literal["merged_unchanged", "edited", "rejected", "reverted", "triaged_by_human", "answered_from_card"]
    triage_corrections: dict[str, Any] = Field(default_factory=dict)


class Word(BaseModel):
    forwarder_term: str
    code_name: str


class ScreenView(BaseModel):
    what_happens: str
    how_people_use_it: str
    words: list[Word] = Field(default_factory=list)
    where_it_breaks: str
    what_changed: str


class Source(BaseModel):
    kind: str  # e.g. "posthog" | "pr" | "code" | "slack"
    ref: str


class ScreenCard(BaseModel):
    screen: str
    route: str
    forwarder: ScreenView
    developer: ScreenView
    sources: list[Source] = Field(default_factory=list)


class TraceEvent(BaseModel):
    node: str
    kind: Literal["tool", "llm", "python", "decision", "human", "graph"]
    input: Any = None
    output: Any = None
    p: float | None = None
    retry: int = 0
    ts: float

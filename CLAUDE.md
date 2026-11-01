# Feedback Autopilot

A background agent that turns forwarder feedback (Slack message + screenshot) and nightly PostHog heatmap data into triaged issues, failing tests, fixes and PRs. Proven kinds of fixes auto-merge. Everything else waits for a developer.

Design reference (problem, architecture, build plan, demo): https://claude.ai/artifact/QKr9qCZ2tkA5PBoWMMfEVn

Hackathon: 2026-11-01, build deadline 18:30, then a live demo (no slides).

## Stack

Python 3.11+, uv, LangGraph (+ `langgraph-checkpoint-sqlite`), Pydantic, Pydantic AI, Claude Agent SDK, FastAPI, Streamlit, SQLite, pytest + Playwright.

## Rules

1. The graph edges are fixed in `app/graph.py`. The LLM runs only inside these nodes: extract issue, reproduce (writes the test), fix (`code_fix()`), and the PR / reply text.
2. Every decision goes through `decide(question, options, context) -> {answer, p}` in `app/decide.py`. Backend from `DECIDE_BACKEND`: `mock` reads `data/decide_fixtures.json` keyed by `signal_id` + question, and falls back to `llm` when no fixture exists; `llm` uses structured output; `jev` calls the Jev API. No graph code depends on the backend.
3. Every LLM call goes through `llm(task, schema)` in `app/llm.py` and returns a Pydantic model. The model comes from `LLM_MODEL`.
4. `code_fix()` runs in a git worktree under `.worktrees/` on branch `fix/<signal-id>`. It may edit files in `target-app/` only, and never existing tests. Verify rejects any diff outside `target-app/` or one that changes an existing test.
5. Plain Python does all checks: running tests and lint, diff stats, heatmap grouping and scoring, merge gate rules. No LLM in the gate.
6. Tools start as plain functions in `app/tools/`. Their names match the MCP tool names below, so wrapping them in FastMCP later is mechanical.
7. Every node is visible in the live trace: the UI reads `graph.stream(..., stream_mode="updates")` and shows node, kind (tool, LLM, Python, decision, human), input, output, `p`, retries and the gate result.
8. Developer review uses LangGraph `interrupt()` with the SQLite checkpointer; the UI resumes the graph with the decision.
9. Secrets live in `.env` only. Never commit `.env`.

## Graph

collect signal → enrich → extract issue → triage → locate code → reproduce → fix → verify → open PR → merge gate → developer review → merge and notify

- Triage: duplicate → link duplicate (add evidence). Not fixable → open issue with evidence.
- Reproduce: the new test must fail. If it passes, go back to enrich (max `MAX_RETRIES`), then open issue.
- Verify: on failure go back to fix (max `MAX_RETRIES`), then open issue with the attempt attached.
- Merge gate: trusted → auto-merge (skip review). Otherwise developer review; the decision is saved as a label.

## Tools

| Module | Functions |
|---|---|
| `slack` | `get_feedback`, `post_reply` |
| `posthog` | `get_session_events`, `query_events`, `get_heatmap` |
| `repo` | `search_code`, `read_file`, `create_worktree`, `run_tests`, `diff_stats` |
| `github` | `open_pr`, `merge_pr`, `request_review`, `create_issue`, `revert_pr` (wrap the `gh` CLI) |
| `labels` | `find_duplicates`, `get_similar_cases`, `get_trust`, `save_label` |

Today `slack` and `posthog` read fixtures and SQLite. `github` acts on this repo for real.

## Schemas (`app/schemas.py`)

- `Signal`: id, source (`feedback` | `heatmap`), text, screenshot path, user, page URL, timestamp
- `Evidence`: session events, rage/dead clicks, console and network errors, element selector, heatmap group stats
- `Issue`: page, element, expected, actual, steps to reproduce, candidate files
- `Triage`: duplicate_of, category (`bug` | `copy` | `ux` | `feature_request` | `how_to`), code area, severity, fixable, each with `p`
- `FixAttempt`: branch, test path, test failed before, tests pass after, lint ok, diff lines, files, touched protected paths
- `PullRequest`: number, url, branch, gate result, merged by (`auto` | developer)
- `Label`: signal id, category, code area, outcome (`merged_unchanged` | `edited` | `rejected` | `reverted`), triage corrections

## Merge gate (`app/gate.py`)

Auto-merge only when all pass, else developer review:

- new test failed before the fix and passes after
- full suite and lint pass
- diff ≤ `MAX_DIFF_LINES` lines in ≤ `MAX_DIFF_FILES` files
- no protected paths (anything outside `target-app/`, plus billing, auth, migrations, CI)
- triage and fix `p` ≥ `CONFIDENCE_THRESHOLD`
- category · code area trusted: ≥ `TRUST_MIN_CASES` labels, ≥ `TRUST_MIN_UNCHANGED` merged unchanged, 0 reverts
- not picked by the random spot check (`SPOT_CHECK_RATE`)

## Nightly (`app/heatmap.py`, `evals/run.py`)

1. Query `$rageclick`, `$dead_click`, `$exception` for the last 24 h.
2. Group by page + element selector (+ error message for exceptions).
3. Score = users affected × events, against the previous 7 days. New and rising first.
4. Top N (default 3), skipping groups already open as an issue or PR. Each becomes a `Signal` with `source="heatmap"`.
5. Impact check: for each merged fix (agent or developer), compare the triggering signal before vs after the deploy, plus new errors on the same screen. Worse → revert PR for a developer, label `reverted`. Demo: PostHog mock has after-fix data, FB-101 dead clicks 14 → 0, prints "FB-101 fix confirmed".
6. Evals replay all labels, write scores, rebuild the trust table. Any revert makes that category · area untrusted.

Limits: `MAX_AUTO_MERGES_PER_DAY` (default 5), one open agent PR per code area.

## Demo data (`data/generate.py`)

Target app `target-app/` is a small forwarder portal in plain HTML/JS with a Feedback button (html2canvas screenshot → `POST /feedback`). Planted problems:

| ID | Problem | Category · area | Expected path |
|---|---|---|---|
| P1 | Download PDF on the invoice page throws `TypeError` (handler looks up the wrong element id) | bug · handler-frontend | PR, developer review (not trusted) |
| P2 | Shipments table header says "Weight (lbs)" but values are kg | copy · frontend | PR, auto-merge after the nightly eval run |
| P3 | ETA cell looks clickable, users rage-click it expecting tracking details | ux | GitHub issue for product |

Fixtures:

- PostHog events for 3 days in PostHog's event shape, with P1 (6 users, 14 dead clicks + `TypeError`) and P3 (9 users, 41 rage clicks) hidden in normal traffic.
- Feedback FB-101 (P1, "Download button does nothing") and FB-102 (P2, "Weight column says lbs, but these are kg") arrive live during the demo.
- `decide_fixtures.json`: FB-101 bug p 0.94, fixable p 0.88; FB-102 copy p 0.97; heatmap ETA group ux p 0.90, not fixable.
- Label store: 150 past decisions; 40 are copy · frontend, all merged unchanged; 12 are handler · frontend. Trust table starts empty.

## Mocks

Slack, PostHog and Jev are mocked in the shape of the real services. The LLM, coding agent, tests and GitHub PRs are real.

- Slack: Feedback button → `POST /feedback` stores a Slack message event (text, user, channel, file, ts) in SQLite.
- PostHog: `data/posthog_events.json`, events `$rageclick`, `$dead_click`, `$exception`, `$autocapture` with `$elements_chain` selector, `$current_url`, `distinct_id`, `$session_id`, timestamp.
- Jev: `decide()` with `DECIDE_BACKEND=mock`.

## Shared map (`app/map_builder.py`)

Runs in the nightly trigger after the heatmap scan. One `ScreenCard` per screen (3 screens: shipments, invoice, booking):

- list screens (routes + PostHog pages) → usage in Python (visits, top 3 paths, hotspots) → collect feedback, labels, PRs and files for the screen → `llm("screen_card", ScreenCard)` → save if changed.
- `ScreenCard` has `forwarder` and `developer` views of: what happens here, how people use it, words (forwarder term ↔ code name), where it breaks, what changed. Every fact has a `source`.
- Triage category `how_to` → answer the feedback from the card, no developer.
- Streamlit tab with a Forwarder / Developer toggle.

## Demo moments

1. FB-101 → graph → PR → gate asks a developer → approve → merged → button works → Slack reply.
2. Run nightly → ETA group becomes a product issue → evals → copy · frontend trusted.
3. FB-102 → graph → PR → auto-merged, no developer.
4. Shared map → invoice screen card, toggle forwarder / developer; it lists today's fix.

## Milestones (deadline 18:30)

- M0 15:35–16:00 mocks + target app
- M1 16:00–16:40 signal → triaged issue, live trace
- M2 16:40–17:20 reproduce, fix, verify, real PR
- M3 17:20–17:50 gate, review, nightly, evals
- M4 17:50–18:10 shared map
- M5 18:10–18:30 reset script, rehearse

Never cut: real fix + PR, FB-102 auto-merged after nightly, one Screen Card in both views.

A reset script restores P1 and P2 and closes demo PRs so the demo can run again.

## Layout

```
data/        generate.py, decide_fixtures.json, seed.sqlite (generated)
app/         schemas, tools/, llm, decide, coder, heatmap, gate, graph, api
evals/       run.py
target-app/  index.html, app.js, feedback.js, tests/
ui/          streamlit_app.py (left: signals, review, Run nightly · right: live trace)
```

## Commands

```
uv sync
uv run playwright install chromium
uv run python data/generate.py
uv run uvicorn app.api:app --reload          # feedback intake + serves target-app
uv run streamlit run ui/streamlit_app.py
uv run pytest target-app/tests
uv run python evals/run.py
```

## Two builders

Abubakar pushes the boilerplate (skeleton, schemas, tool stubs, `run_signal`/`resume`) by 16:10. Nils starts at once on `target-app/` and `data/`, which need no boilerplate. Demo runs on Abubakar's laptop.

- **Abubakar · boilerplate + agent** owns `app/graph.py`, `llm.py`, `decide.py`, `coder.py`, `gate.py`, `tools/repo.py`, `tools/github.py`.
- **Nils · world** owns `target-app/`, `data/`, `app/api.py`, `tools/slack.py`, `tools/posthog.py`, `tools/labels.py`, `heatmap.py`, `map_builder.py`, `evals/`, `ui/`, `scripts/`.
- Shared: `app/schemas.py`, `CLAUDE.md`. Change only in a PR of its own, after telling the other person.
- Seams: `run_signal(signal_id)` and `resume(signal_id, decision)` yield trace events `{node, kind, input, output, p, retry, ts}`. Every tool starts as a stub returning one hard-coded example.
- Branches `abubakar/<task>`, `nils/<task>`; merge to `main` at least every 30 min. Agent demo PRs target `GITHUB_BASE_BRANCH=demo`; `scripts/reset.sh` resets `demo` to `main`. Planted bug lines P1–P3 are frozen after M0.
- `data/generate.py` uses a fixed random seed; `seed.sqlite` is not committed.

## Local testing

1. `uv run pytest tests` — schemas, gate, heatmap scoring, impact check, decide mock. No LLM, no network. Before every push.
2. `uv run pytest target-app/tests` — existing Playwright tests pass with the planted bugs.
3. `DRY_RUN=1 CODER_BACKEND=stub uv run python -m app.graph FB-101` — full graph; GitHub writes to `.dry_run/`, coder applies `data/patches/<signal>.diff`.
4. `scripts/demo_check.sh` — reset → FB-101 PR → approve → nightly → FB-102 auto-merged → impact confirmed; non-zero exit at the first failing step.

Services: `uvicorn app.api:app` on :8000 (target app at `/app`), Streamlit on :8501.

## Git

Never add `Co-Authored-By` or any other AI attribution to commits or PR descriptions. `.claude/settings.json` turns it off for everyone who uses this repo.

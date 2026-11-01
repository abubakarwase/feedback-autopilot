# Feedback Autopilot

Forwarders report problems through a Feedback button, and PostHog records where every user struggles. Today a developer reads each Slack message by hand, finds the session, reproduces the bug and fixes it.

Feedback Autopilot does that first hour in the background:

1. Reads the feedback (text + screenshot) or a nightly heatmap hotspot.
2. Pulls the user's PostHog session: clicks, rage and dead clicks, console errors.
3. Triages it: duplicate, category, severity, fixable.
4. Writes a test that reproduces it, fixes the code in a worktree, and runs the suite.
5. Opens a PR with the evidence.
6. Auto-merges proven kinds of fixes. Everything else waits for a developer.
7. Tells the forwarder it's fixed.

Every developer decision is saved as a label. A nightly eval run turns the labels into a trust table that decides which kinds of fixes may merge alone.

Design: https://claude.ai/artifact/QKr9qCZ2tkA5PBoWMMfEVn (local copy: `docs/design.html`)

## Quick start

```
cp .env.example .env    # add your keys
uv sync
uv run playwright install chromium
uv run python data/generate.py
uv run uvicorn app.api:app --reload
uv run streamlit run ui/streamlit_app.py
```

See `CLAUDE.md` for the stack, rules and layout.

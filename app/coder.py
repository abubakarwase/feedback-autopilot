"""Coding agent: fixes the issue inside <worktree>/target-app."""
import subprocess
from pathlib import Path

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.usage import UsageLimits

from app import config
from app.schemas import Issue
from app.tools import repo

PROMPT = """You fix one bug in a small web app. Make the smallest change that fixes the issue.
The failing repro test at {test} must pass after your change. Never edit existing tests.
All paths are relative to target-app/ (a target-app/ prefix is accepted). Use list_files, read_file, write_file (full file content) and run_tests. Finish with a one-sentence summary."""


def _stub(signal_id: str, worktree: str, root: str) -> dict:
    patch = (Path(root) / "data" / "patches" / f"{signal_id}.diff").resolve()
    if not patch.is_file():
        return {"ok": False, "summary": "no stub patch", "steps": 0}
    r = subprocess.run(["git", "apply", str(patch)], cwd=worktree, capture_output=True, text=True)
    return {"ok": r.returncode == 0, "summary": f"applied {patch.name}" if r.returncode == 0 else r.stderr.strip(),
            "steps": 0}


def code_fix(issue: Issue, files: list[str], worktree: str, signal_id: str,
             test_path: str | None = None, root: str = ".") -> dict:
    cfg = config.settings
    if cfg.CODER_BACKEND == "stub" or cfg.OFFLINE:
        return _stub(signal_id, worktree, root)

    app_dir = (Path(worktree) / repo.TARGET).resolve()

    def safe(rel: str) -> Path:
        rel = rel.removeprefix(repo.TARGET + "/")  # accept repo-relative paths too
        p = (app_dir / rel).resolve()
        if app_dir not in p.parents:
            raise ModelRetry(f"path outside {repo.TARGET}/: {rel}")
        return p

    agent = Agent(cfg.CODER_MODEL, system_prompt=PROMPT.format(test=test_path or "target-app/tests"))

    @agent.tool_plain
    def list_files() -> list[str]:
        """List files of target-app (relative paths)."""
        return sorted(p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*")
                      if p.is_file() and not set(p.relative_to(app_dir).parts) & repo.SKIP_DIRS)

    @agent.tool_plain
    def read_file(path: str) -> str:
        """Read a file of target-app."""
        p = safe(path)
        if not p.is_file():
            raise ModelRetry(f"no such file: {path}")
        return p.read_text()

    @agent.tool_plain
    def write_file(path: str, content: str) -> str:
        """Overwrite or create a source file of target-app. Tests and test config are read-only."""
        p = safe(path)
        parts = p.relative_to(app_dir).parts
        if parts[0].lower() == "tests" or p.name.lower() in repo.CONFIG_NAMES:
            return "refused: tests and test config must not be written"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return "ok"

    @agent.tool_plain
    def run_tests(test_path: str | None = None) -> dict:
        """Run pytest (default: all target-app tests); path relative to the repo root, e.g. target-app/tests/x.py."""
        if test_path and not (test_path.startswith(repo.TARGET + "/") and ".." not in test_path):
            raise ModelRetry(f"test path must be under {repo.TARGET}/")
        r = repo.run_tests(worktree, test_path)
        return {"passed": r["passed"], "output": r["output"]}

    prompt = (f"Issue: {issue.model_dump_json()}\nCandidate files: {files}\nRepro test: {test_path}")
    try:
        from app.llm import run_agent
        res = run_agent(agent, prompt, usage_limits=UsageLimits(request_limit=8))
    except Exception as e:  # usage limit, API error
        return {"ok": False, "summary": f"coder failed: {e}", "steps": 0}
    return {"ok": True, "summary": str(res.output), "steps": res.usage.requests}

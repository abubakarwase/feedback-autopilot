"""Settings from environment (.env loaded with python-dotenv)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    # strip inline "# comment" from values like "mock   # mock | real"
    return os.environ.get(name, default).split("#", 1)[0].strip()


@dataclass(frozen=True)
class Settings:
    LLM_MODEL: str
    CODER_MODEL: str
    DECIDE_BACKEND: str
    CONFIDENCE_THRESHOLD: float
    MAX_RETRIES: int
    MAX_DIFF_LINES: int
    MAX_DIFF_FILES: int
    SPOT_CHECK_RATE: float
    MAX_AUTO_MERGES_PER_DAY: int
    TRUST_MIN_CASES: int
    TRUST_MIN_UNCHANGED: float
    GITHUB_BACKEND: str
    GITHUB_BASE_BRANCH: str
    DRY_RUN: bool
    CODER_BACKEND: str
    DB_PATH: str
    OFFLINE: bool
    CHECKPOINT_PATH: str


def load() -> Settings:
    return Settings(
        LLM_MODEL=_env("LLM_MODEL", "google:gemini-3.8-flash"),
        CODER_MODEL=_env("CODER_MODEL", "google:gemini-3.1-pro-preview"),
        DECIDE_BACKEND=_env("DECIDE_BACKEND", "mock"),
        CONFIDENCE_THRESHOLD=float(_env("CONFIDENCE_THRESHOLD", "0.85")),
        MAX_RETRIES=int(_env("MAX_RETRIES", "2")),
        MAX_DIFF_LINES=int(_env("MAX_DIFF_LINES", "40")),
        MAX_DIFF_FILES=int(_env("MAX_DIFF_FILES", "3")),
        SPOT_CHECK_RATE=float(_env("SPOT_CHECK_RATE", "0.05")),
        MAX_AUTO_MERGES_PER_DAY=int(_env("MAX_AUTO_MERGES_PER_DAY", "5")),
        TRUST_MIN_CASES=int(_env("TRUST_MIN_CASES", "30")),
        TRUST_MIN_UNCHANGED=float(_env("TRUST_MIN_UNCHANGED", "0.95")),
        GITHUB_BACKEND=_env("GITHUB_BACKEND", "mock"),
        GITHUB_BASE_BRANCH=_env("GITHUB_BASE_BRANCH", "demo"),
        DRY_RUN=_env("DRY_RUN", "0") not in ("0", "", "false", "False"),
        CODER_BACKEND=_env("CODER_BACKEND", "gemini"),
        DB_PATH=_env("DB_PATH", "data/seed.sqlite"),
        OFFLINE=_env("OFFLINE", "0") not in ("0", "", "false", "False"),
        CHECKPOINT_PATH=_env("CHECKPOINT_PATH", ".checkpoints.sqlite"),
    )


settings = load()

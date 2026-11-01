"""Merge gate: pure function, no I/O."""
import hashlib

from app import config
from app.schemas import FixAttempt, Triage


def _spot_checked(signal_id: str) -> bool:
    h = int(hashlib.sha1(signal_id.encode()).hexdigest(), 16) % 1000 / 1000
    return h < config.settings.SPOT_CHECK_RATE


def merge_gate(fix: FixAttempt, triage: Triage, trust: dict, signal_id: str, merges_today: int) -> dict:
    cfg = config.settings
    checks = {
        "reproduced": fix.test_failed_before,
        "suite_and_lint": fix.tests_pass_after and fix.lint_ok,
        "diff_small": fix.diff_lines <= cfg.MAX_DIFF_LINES and len(fix.files) <= cfg.MAX_DIFF_FILES,
        "no_protected_paths": not fix.touched_protected_paths,
        "confidence": min(triage.p, fix.p) >= cfg.CONFIDENCE_THRESHOLD,
        "trusted": bool(trust.get("trusted")),
        "not_spot_checked": not _spot_checked(signal_id),
        "daily_limit": merges_today < cfg.MAX_AUTO_MERGES_PER_DAY,
    }
    reasons = [k for k, v in checks.items() if not v]
    return {"auto_merge": not reasons, "reasons": reasons, "checks": checks}

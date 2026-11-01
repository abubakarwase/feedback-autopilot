#!/usr/bin/env bash
# Restore the demo: demo branch + .worktrees/demo at main, no fix/* branches or worktrees,
# fresh checkpoint DB, no screenshots, regenerated data (signals, labels, trust, cards).
# Usage: scripts/reset.sh            (DEMO_BASE=<ref> to reset demo to something other than main)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd -P)
BASE=${DEMO_BASE:-main}
DEMO_DIR="$ROOT/.worktrees/demo"

say() { echo "[reset] $*"; }
die() { echo "[reset] ERROR: $*" >&2; exit 1; }

for port in 8000 8501; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
    die "port $port is in use (api/streamlit running?). Stop it first: running processes keep the old checkpoint DB open."
  fi
done

git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null || die "base ref '$BASE' does not exist"

# Settings as the app sees them (.env included).
paths=$(uv run python -c "
from pathlib import Path
from app import config
s = config.settings
if Path(s.DB_PATH).resolve() != Path('data/seed.sqlite').resolve():
    raise SystemExit(f'DB_PATH={s.DB_PATH}, but data/generate.py only writes data/seed.sqlite')
print(s.CHECKPOINT_PATH)
") || die "could not read settings (see error above)"
CHECKPOINT=$(printf '%s\n' "$paths" | tail -n 1)

# "<path>\t<branch ref>" for every worktree that has a branch checked out
worktrees() { git worktree list --porcelain | awk '/^worktree /{p=substr($0,10)} /^branch /{print p "\t" substr($0,8)}'; }

git worktree prune

# 1. fix/* worktrees and branches
worktrees | while IFS=$'\t' read -r path ref; do
  case "$ref" in
    refs/heads/fix/*) git worktree remove --force "$path"; say "removed worktree $path (${ref#refs/heads/})" ;;
  esac
done
for b in $(git for-each-ref --format='%(refname:short)' refs/heads/fix/); do
  git branch -D "$b" >/dev/null
  say "deleted branch $b"
done
for d in .worktrees/fix-* .worktrees/fix; do
  if [ -e "$d" ]; then rm -rf "$d"; say "deleted leftover dir $d"; fi
done
git worktree prune

# 2. demo branch at BASE, checked out in .worktrees/demo
demo_wt=$(worktrees | awk -F'\t' '$2 == "refs/heads/demo" {print $1}')
if [ -z "$demo_wt" ]; then
  if git worktree list --porcelain | grep -Fxq "worktree $DEMO_DIR"; then
    git worktree remove --force "$DEMO_DIR"   # registered on another branch / detached
  elif [ -e "$DEMO_DIR" ]; then
    rm -rf "$DEMO_DIR"                        # stale, unregistered dir
  fi
  git branch -f demo "$BASE" >/dev/null
  git worktree add --quiet "$DEMO_DIR" demo
  say "demo branch set to $BASE ($(git rev-parse --short "$BASE")), worktree .worktrees/demo created"
elif [ "$demo_wt" = "$DEMO_DIR" ]; then
  git -C "$DEMO_DIR" reset --quiet --hard "$BASE"
  git -C "$DEMO_DIR" clean -fdq
  say "demo worktree reset to $BASE ($(git rev-parse --short "$BASE"))"
else
  die "branch demo is checked out at $demo_wt, expected $DEMO_DIR. Check out another branch there, then rerun."
fi

# 3. runtime state
for f in "$CHECKPOINT" "$CHECKPOINT-wal" "$CHECKPOINT-shm" "$CHECKPOINT-journal"; do
  if [ -e "$f" ]; then rm -f "$f"; say "deleted $f"; fi
done
rm -rf data/screenshots && say "cleared data/screenshots/"
if [ -e .dry_run ]; then rm -rf .dry_run; say "cleared .dry_run/"; fi

# 4. data (rewrites data/seed.sqlite: signals, replies, labels, trust, eval runs, impact checks, cards)
uv run python data/generate.py | sed 's/^/[reset]   /'
say "data regenerated"

[ -d "$DEMO_DIR/target-app" ] || die "$BASE has no target-app/, so the demo app would serve the unfixed root copy. Merge the target-app work into $BASE, then rerun."
say "done: demo app serves from .worktrees/demo/target-app"
say "demo order (ids follow arrival): FB-101 download -> run, approve | post FB-102 weight, do not run | FB-103 weird -> run, triage | Run nightly | run FB-102"

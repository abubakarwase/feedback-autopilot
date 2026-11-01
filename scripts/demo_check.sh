#!/usr/bin/env bash
# End-to-end demo rehearsal without the UI, in the live demo order.
# Feedback ids are handed out in arrival order (FB-101, FB-102, ...) and decide fixtures are keyed by id, so the
# live demo MUST post in this order:
#   1. FB-101 "Download button does nothing" (u-anna, invoice) -> Run -> approve
#   2. FB-102 "Weight column says lbs, but these are kg" (u-ben, shipments) -> post now, do NOT press Run yet
#   3. FB-103 "the shipments page is weird" (u-carla, shipments) -> Run -> pauses at human triage -> ux · frontend
#   4. Run nightly (impact check, evals: copy · frontend trusted)
#   5. Run FB-102 -> auto-merged, no developer
# Never re-post during a rehearsal without scripts/reset.sh in between.
# Exits non-zero at the first failing step. Always stops the api.
# Runs with DRY_RUN=1 CODER_BACKEND=stub; add OFFLINE=1 for no network (llm placeholders).
set -euo pipefail
cd "$(dirname "$0")/.."
export DRY_RUN=1 CODER_BACKEND=stub
PORT=8000
API="http://127.0.0.1:$PORT"
PNG="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

step() { echo; echo "=== [demo_check] $*"; }
fail() { echo "[demo_check] FAIL: $*" >&2; exit 1; }

API_PID=""
stop_api() {
  if [ -n "$API_PID" ]; then
    pkill -TERM -P "$API_PID" 2>/dev/null || true   # uvicorn under uv
    kill -TERM "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap stop_api EXIT

# $1 user, $2 text, $3 page_url, $4 expected signal id
post_feedback() {
  local body resp
  body=$(printf '{"type":"message","channel":"C-FEEDBACK","user":"%s","text":"%s","ts":"%s","files":[{"name":"screenshot.png","mimetype":"image/png","data_url":"%s"}],"page_url":"%s","client_ts":"%s"}' \
    "$1" "$2" "$(date +%s).000100" "$PNG" "$3" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
  resp=$(curl -sS --fail-with-body -X POST "$API/feedback" -H 'Content-Type: application/json' -d "$body") \
    || fail "POST /feedback for $4 failed: $resp"
  echo "$resp"
  case "$resp" in *"\"id\":\"$4\""*) ;; *) fail "POST /feedback returned $resp, expected id $4" ;; esac
}

# $1 signal id, $2 mode: review (expect developer review, approve) | auto (expect auto-merge)
#   | triage (expect human triage, resume ux · frontend, expect open_issue)
run_graph() {
  uv run python - "$1" "$2" <<'PY'
import sys
from app.graph import resume, run_signal

sid, mode = sys.argv[1], sys.argv[2]

def show(events):
    for e in events:
        print(f"  {e.node:<18} {e.kind:<8} p={e.p} retry={e.retry}")
    return events

events = show(list(run_signal(sid)))
if not events:
    sys.exit(f"{sid}: run_signal yielded no events")
if mode == "triage":
    last = events[-1]
    if (last.node, last.kind) != ("human_triage", "human"):
        sys.exit(f"{sid}: expected a pause at human_triage, graph stopped at {last.node} ({last.kind})")
    print(f"  -- resume {sid} with ux · frontend")
    events = show(list(resume(sid, {"category": "ux", "code_area": "frontend"})))
    if events[-1].node != "open_issue":
        sys.exit(f"{sid}: expected open_issue after human triage, graph stopped at {events[-1].node}")
    print(f"  {sid} triaged by a developer -> issue opened")
    sys.exit(0)
if mode == "review":
    last = events[-1]
    if (last.node, last.kind) != ("developer_review", "human"):
        sys.exit(f"{sid}: expected a pause at developer_review, graph stopped at {last.node} ({last.kind})")
    print(f"  -- resume {sid} with approve")
    events = show(list(resume(sid, {"action": "approve"})))
    want_by = "developer"
else:
    if any(e.node == "developer_review" for e in events):
        sys.exit(f"{sid}: expected auto-merge, but the gate sent it to developer_review")
    want_by = "auto"
merged = [e for e in events if e.node == "merge_and_notify"]
if not merged:
    sys.exit(f"{sid}: never reached merge_and_notify (last node: {events[-1].node})")
out = merged[-1].output
if out.get("by") != want_by or out.get("merge", {}).get("state") != "merged":
    sys.exit(f"{sid}: merge_and_notify output {out}, expected merged by {want_by}")
print(f"  {sid} merged by {want_by}")
PY
}

# $1 SQL returning one value, $2 expected value, $3 failure message
assert_db() {
  uv run python - "$1" "$2" "$3" <<'PY'
import sqlite3, sys
from app import config

sql, want, msg = sys.argv[1:]
row = sqlite3.connect(config.settings.DB_PATH).execute(sql).fetchone()
got = None if row is None else str(row[0])
if got != want:
    sys.exit(f"{msg}: got {got!r}, expected {want!r} ({sql})")
print(f"  ok: {sql} -> {got}")
PY
}

step "1/10 reset"
scripts/reset.sh || fail "scripts/reset.sh"

step "2/10 start api on :$PORT (log: logs/demo_check_api.log)"
mkdir -p logs
uv run uvicorn app.api:app --host 127.0.0.1 --port "$PORT" >logs/demo_check_api.log 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  curl -sf "$API/health" >/dev/null 2>&1 && break
  kill -0 "$API_PID" 2>/dev/null || fail "api exited during startup, see logs/demo_check_api.log"
  sleep 0.5
done
curl -sf "$API/health" || fail "api not healthy after 30 s, see logs/demo_check_api.log"
echo

step "3/10 POST FB-101 feedback (invoice, Download PDF)"
post_feedback u-anna "Download button does nothing" "http://localhost:$PORT/app/#/invoices/INV-2291" FB-101

step "4/10 graph FB-101 -> developer review -> approve -> merged"
run_graph FB-101 review || fail "graph FB-101"

step "5/10 POST FB-102 feedback (shipments, Weight lbs) -- not run until after nightly"
post_feedback u-ben "Weight column says lbs, but these are kg" "http://localhost:$PORT/app/#/shipments" FB-102

step "6/10 POST FB-103 feedback (shipments, vague)"
post_feedback u-carla "the shipments page is weird" "http://localhost:$PORT/app/#/shipments" FB-103

step "7/10 graph FB-103 -> low p twice -> human triage -> ux · frontend -> issue"
run_graph FB-103 triage || fail "graph FB-103"

step "8/10 nightly (heatmap scan, impact check, evals, shared map)"
uv run python -m app.nightly || fail "uv run python -m app.nightly"
assert_db "SELECT trusted FROM trust WHERE category='copy' AND code_area='frontend'" 1 \
  "evals did not make copy · frontend trusted" || fail "trust table"

step "9/10 graph FB-102 -> auto-merged, no developer"
run_graph FB-102 auto || fail "graph FB-102"

step "10/10 impact check FB-101 confirmed"
assert_db "SELECT verdict FROM impact_checks WHERE signal_id='FB-101' ORDER BY ts DESC, id DESC LIMIT 1" confirmed \
  "FB-101 impact check not confirmed" || fail "impact check"

echo
echo "[demo_check] PASS"

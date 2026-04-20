#!/usr/bin/env bash
# scripts/verify_v0_5.sh — v0.5 real-machine re-verify runner.
#
# Fires 3 HTTP requests against a running CodeRouter, each of which should
# trigger a `capability-degraded` log line with a distinct `reason`:
#
#   A) /v1/messages  + `thinking` block     → reason: provider-does-not-support
#                                               (v0.5-A, request-side strip + log)
#   B) /v1/messages  + `cache_control`      → reason: translation-lossy
#                                               (v0.5-B, observability-only)
#   C) /v1/chat/completions (gpt-oss:free)  → reason: non-standard-field
#                                               (v0.5-C, response-side strip)
#
# Prereqs:
#   1. `OPENROUTER_API_KEY` set in the server's env (gpt-oss:free is used).
#   2. coderouter running with examples/providers.yaml (which includes the
#      `verify-gpt-oss` profile). Start it in terminal 1:
#
#        coderouter serve --config examples/providers.yaml \
#                         --port 4000 \
#                         2> /tmp/coderouter-verify.log
#
#   3. Run this script in terminal 2:
#
#        bash scripts/verify_v0_5.sh
#
# Exit code 0 iff every scenario saw its expected reason + a 2xx response.
# Per-scenario artifacts land under $VERIFY_OUT_DIR (default
# /tmp/coderouter-verify). A ready-to-paste markdown report is written to
# $VERIFY_OUT_DIR/report.md for the retro doc.

set -u

BASE_URL="${CODEROUTER_URL:-http://127.0.0.1:4000}"
LOG_FILE="${CODEROUTER_LOG:-/tmp/coderouter-verify.log}"
OUT_DIR="${VERIFY_OUT_DIR:-/tmp/coderouter-verify}"
PROFILE="${VERIFY_PROFILE:-verify-gpt-oss}"

mkdir -p "$OUT_DIR"

if [ ! -f "$LOG_FILE" ]; then
  cat >&2 <<EOF
ERROR: log file not found: $LOG_FILE

Start coderouter in another terminal so its stderr (structured JSON logs)
lands in that file. E.g.:

  coderouter serve --config examples/providers.yaml --port 4000 \\
    2> $LOG_FILE

Then re-run this script.
EOF
  exit 2
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

mark_log_pos() { wc -l < "$LOG_FILE" 2>/dev/null | tr -d ' '; }
new_log_lines() { tail -n "+$((${1} + 1))" "$LOG_FILE" 2>/dev/null || true; }
filter_capability_lines() { grep -E '"msg":[[:space:]]*"capability-degraded"' || true; }
json_extract() {  # json_extract '<json>' '<dotted.path>'
  python3 -c '
import json, sys
data = json.loads(sys.argv[1])
for key in sys.argv[2].split("."):
    if isinstance(data, dict):
        data = data.get(key)
    else:
        data = None
        break
print(data if data is not None else "")
' "$1" "$2" 2>/dev/null || echo ""
}

# Long-ish system prompt so cache_control is semantically meaningful.
# (The gate fires before translation anyway, but this lines up with the
# v0.4 retro's 1024-token footgun note and makes the request realistic.)
export _CR_LONG_SYSTEM="$(python3 -c '
text = (
    "You are a rigorous technical assistant. "
    "Answer precisely, cite sources when possible, note uncertainty explicitly. "
)
print(text * 120)
')"

# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

overall_pass=true

# Parallel indexed arrays (bash 3.2 compatible — macOS ships bash 3.2, no
# associative arrays). Kept in lock-step via the scenario index.
SCENARIO_NAMES=()
SCENARIO_RESULTS=()
SCENARIO_STATUSES=()
SCENARIO_CAP_LINES=()
SCENARIO_EXPECTED=()

run_scenario() {
  local name="$1"          # e.g. A-thinking
  local endpoint="$2"      # /v1/messages
  local body="$3"          # JSON string
  local expect_reason="$4" # provider-does-not-support | translation-lossy | non-standard-field
  local dir="$OUT_DIR/$name"

  mkdir -p "$dir"
  echo "$body" > "$dir/request.json"

  local log_pos_before
  log_pos_before=$(mark_log_pos)

  local http_status
  http_status=$(curl -sS -o "$dir/response.json" -w "%{http_code}" \
    -X POST "${BASE_URL}${endpoint}" \
    -H "Content-Type: application/json" \
    -H "x-coderouter-profile: $PROFILE" \
    --data-binary @"$dir/request.json" 2>"$dir/curl-stderr.txt" || echo "000")

  sleep 0.5  # allow log handler flush

  new_log_lines "$log_pos_before" > "$dir/server-log-slice.jsonl"

  local cap_lines
  cap_lines=$(filter_capability_lines < "$dir/server-log-slice.jsonl")
  echo "$cap_lines" > "$dir/capability-degraded.jsonl"

  # Pass: 2xx + at least one capability-degraded line with the expected reason.
  local pass=true
  [[ "$http_status" =~ ^2 ]] || pass=false
  echo "$cap_lines" | grep -q "\"reason\":[[:space:]]*\"$expect_reason\"" || pass=false

  local result
  if $pass; then result=PASS; else result=FAIL; overall_pass=false; fi

  SCENARIO_NAMES+=("$name")
  SCENARIO_RESULTS+=("$result")
  SCENARIO_STATUSES+=("$http_status")
  SCENARIO_CAP_LINES+=("$cap_lines")
  SCENARIO_EXPECTED+=("$expect_reason")

  # Pretty print
  echo "=============================================="
  echo " SCENARIO: $name"
  echo "   endpoint:    $endpoint"
  echo "   profile:     $PROFILE"
  echo "   expect:      reason=$expect_reason"
  echo "   HTTP status: $http_status"
  echo "   result:      $result"
  echo "----------------------------------------------"
  echo "capability-degraded lines observed:"
  if [ -n "$cap_lines" ]; then
    echo "$cap_lines" | sed 's/^/   /'
  else
    echo "   (none)"
  fi
  echo "----------------------------------------------"
  echo "Artifacts under $dir :"
  echo "   request / response / server-log-slice / capability-degraded"
  echo ""
}

# Look up a per-scenario value by name. Returns empty string if not found.
lookup_scenario() {
  local key="$1" name="$2"
  local i
  for i in $(seq 0 $((${#SCENARIO_NAMES[@]} - 1))); do
    if [ "${SCENARIO_NAMES[$i]}" = "$name" ]; then
      case "$key" in
        result)   echo "${SCENARIO_RESULTS[$i]}" ;;
        status)   echo "${SCENARIO_STATUSES[$i]}" ;;
        cap)      echo "${SCENARIO_CAP_LINES[$i]}" ;;
        expected) echo "${SCENARIO_EXPECTED[$i]}" ;;
      esac
      return 0
    fi
  done
  echo ""
}

# ---------------------------------------------------------------------------
# Scenario bodies (generated via python for robust JSON)
# ---------------------------------------------------------------------------

A_BODY=$(python3 -c '
import json
print(json.dumps({
    "model": "claude-sonnet-4-6",
    "max_tokens": 64,
    "thinking": {"type": "enabled", "budget_tokens": 1024},
    "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
}))
')

B_BODY=$(python3 -c '
import json, os
long_sys = os.environ["_CR_LONG_SYSTEM"]
print(json.dumps({
    "model": "claude-sonnet-4-6",
    "max_tokens": 32,
    "system": [
        {"type": "text", "text": long_sys, "cache_control": {"type": "ephemeral"}}
    ],
    "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
}))
')

C_BODY=$(python3 -c '
import json
print(json.dumps({
    "model": "openai/gpt-oss-120b:free",
    "max_tokens": 128,
    "messages": [
        {"role": "user", "content": "Think briefly, then answer: What is 7 * 8?"}
    ],
}))
')

# ---------------------------------------------------------------------------
# Run scenarios
# ---------------------------------------------------------------------------

run_scenario "A-thinking"       "/v1/messages"         "$A_BODY" "provider-does-not-support"
run_scenario "B-cache-control"  "/v1/messages"         "$B_BODY" "translation-lossy"
run_scenario "C-reasoning-strip" "/v1/chat/completions" "$C_BODY" "non-standard-field"

# ---------------------------------------------------------------------------
# Additional assertion for scenario C: response MUST NOT carry `reasoning`
# (the strip happens on the way out; verify it's gone).
# ---------------------------------------------------------------------------

c_resp="$OUT_DIR/C-reasoning-strip/response.json"
c_reasoning_stripped=true
if [ -f "$c_resp" ]; then
  if ! python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    choices = d.get("choices") or []
    for c in choices:
        if "reasoning" in (c.get("message") or {}):
            sys.exit(1)
    sys.exit(0)
except Exception:
    sys.exit(2)
' "$c_resp"; then
    c_reasoning_stripped=false
  fi
else
  c_reasoning_stripped=false
fi

if $c_reasoning_stripped; then
  echo "[C] response body confirmed: no 'reasoning' key in choices[*].message — strip effective."
else
  echo "[C] WARN: response body still contains 'reasoning' key (or response missing / malformed). Strip may not have fired."
  overall_pass=false
  # Rewrite the C row's RESULT to FAIL (scan SCENARIO_NAMES).
  for i in $(seq 0 $((${#SCENARIO_NAMES[@]} - 1))); do
    if [ "${SCENARIO_NAMES[$i]}" = "C-reasoning-strip" ]; then
      SCENARIO_RESULTS[$i]=FAIL
      break
    fi
  done
fi

# ---------------------------------------------------------------------------
# Generate markdown report for the retro doc
# ---------------------------------------------------------------------------

REPORT="$OUT_DIR/report.md"
{
  echo "## v0.5 real-machine verify — $(date +%Y-%m-%d) ($(date +%H:%M:%S) $(date +%Z))"
  echo ""
  echo "Runner: \`scripts/verify_v0_5.sh\`. Profile: \`$PROFILE\` (openai_compat-only)."
  echo "Base URL: \`$BASE_URL\`. Log: \`$LOG_FILE\`."
  echo ""
  echo "### Summary"
  echo ""
  echo "| Scenario | Gate | Endpoint | Expected \`reason\` | HTTP | Result |"
  echo "|---|---|---|---|---|---|"
  echo "| A-thinking | v0.5-A | \`/v1/messages\` + \`thinking\` | \`provider-does-not-support\` | $(lookup_scenario status A-thinking) | $(lookup_scenario result A-thinking) |"
  echo "| B-cache-control | v0.5-B | \`/v1/messages\` + \`cache_control\` | \`translation-lossy\` | $(lookup_scenario status B-cache-control) | $(lookup_scenario result B-cache-control) |"
  echo "| C-reasoning-strip | v0.5-C | \`/v1/chat/completions\` | \`non-standard-field\` | $(lookup_scenario status C-reasoning-strip) | $(lookup_scenario result C-reasoning-strip) |"
  echo ""
  for name in A-thinking B-cache-control C-reasoning-strip; do
    echo "### ${name} — $(lookup_scenario result "$name")"
    echo ""
    echo "HTTP \`$(lookup_scenario status "$name")\`. Observed \`capability-degraded\` lines:"
    echo ""
    echo '```json'
    cap="$(lookup_scenario cap "$name")"
    if [ -n "$cap" ]; then
      echo "$cap"
    else
      echo "(none)"
    fi
    echo '```'
    echo ""
  done
  echo "### Overall"
  echo ""
  if $overall_pass; then
    echo "**PASS** — all 3 gates fired with the expected \`reason\` on live traffic. v0.5's capability-degraded contract holds against real providers."
  else
    echo "**FAIL** — see per-scenario output. At least one gate did not fire as expected or the HTTP call errored. Inspect artifacts under \`$OUT_DIR/\`."
  fi
} > "$REPORT"

echo "=============================================="
echo " OVERALL: $($overall_pass && echo PASS || echo FAIL)"
echo ""
echo " Markdown report: $REPORT"
echo " Artifacts tree:  $OUT_DIR/"
echo "=============================================="

$overall_pass

#!/usr/bin/env bash
# scripts/demo_traffic.sh -- Dashboard demo traffic generator.
#
# Fires a continuous, varied mix of requests at a running CodeRouter
# server so every panel on GET /dashboard lights up:
#
#   Providers          attempts / OK / fail rows fill in
#   Fallback & Gates   fallback_rate + paid-gate-blocked move
#   Requests / min     sparkline gets peaks (bursts) and plateaus (steady)
#   Recent Events      mix of stream:true/false, try-provider, provider-ok
#   Usage Mix          colored bar splits across providers that answered
#
# Loop-style -- runs until Ctrl+C (or --duration expires). Each "tick"
# picks one of four scenarios at random (weighted so normal + stream
# dominate). Per-request status prints on stderr so you can watch it
# next to the browser, and a summary line with elapsed time + scenario
# counts prints every DEMO_STATUS_EVERY ticks.
#
# Usage:
#   ./scripts/demo_traffic.sh                            # traffic only, Ctrl+C to stop
#   ./scripts/demo_traffic.sh --serve                    # launches server too
#   ./scripts/demo_traffic.sh --duration 5m              # auto-stop after 5 minutes
#   ./scripts/demo_traffic.sh --serve --duration 120s
#   ./scripts/demo_traffic.sh --dry-run --duration 5m    # show plan, don't fire
#   CODEROUTER_URL=http://host:port ./scripts/demo_traffic.sh
#
#   --duration accepts: plain seconds ("90"), "30s", "5m", "1h".
#
# Scenario catalog (picked at each tick):
#   name          pick weight  requests/tick  profile                stream
#   -------------------------------------------------------------------------
#   normal        4 / 10       1              $DEMO_PROFILE          false
#   stream        3 / 10       1              $DEMO_PROFILE          true
#   burst+idle    2 / 10       4 (3 bg + 1)   $DEMO_PROFILE          mixed
#   fallback      1 / 10       $BURST_SIZE    $DEMO_FALLBACK_PROFILE true
#   paid-gate     every 8th    1              $DEMO_PAID_PROFILE     false
#
# The paid-gate scenario preempts the weighted pick on every 8th tick,
# so the effective weights over a long run are:
#   paid-gate  12.5%, normal  35%, stream  26%, burst+idle  17.5%, fallback  8.75%
#
# With --serve the script:
#   1. starts `coderouter serve` in the background (using the knobs below)
#   2. tails its stdout/stderr to $DEMO_SERVE_LOG
#   3. waits for /healthz to return 200 (timeout $DEMO_SERVE_WAIT_S)
#   4. drives traffic until Ctrl+C
#   5. kills the server on exit so there are no orphan processes
#
# Env overrides (all optional):
#   CODEROUTER_URL          base URL   (default http://127.0.0.1:4000;
#                           when --serve is set, derived from DEMO_SERVE_PORT)
#   DEMO_PROFILE            profile for the normal / stream / burst / steady
#                           scenarios (default free-only)
#   DEMO_FALLBACK_PROFILE   profile used to FORCE fallback -- points at a
#                           chain with paid tier at the end, and we burst
#                           it fast enough that the free-cloud rate limit
#                           trips, cascading down to the paid tier -> which
#                           gets paid-gate-blocked (bumps
#                           chain_paid_gate_blocked_total too)
#                           (default coding)
#   DEMO_PAID_PROFILE       profile that contains ONLY paid providers.
#                           Hitting this fires paid-gate-block on the
#                           first try instead of relying on cascading
#                           rate-limits. If the profile doesn't exist on
#                           the server, the scenario silently degrades
#                           to the cascade-burst fallback approach above
#                           (default paid-only-demo -- see NOTE at bottom)
#   DEMO_TICK_SLEEP         seconds between ticks (default 4)
#   DEMO_BURST_SIZE         number of concurrent requests in a burst
#                           (default 5)
#   DEMO_MAX_TOKENS         max_tokens cap on each request (default 48)
#
#   --serve only:
#   DEMO_SERVE_BIN          how to invoke coderouter  (default "uv run coderouter")
#   DEMO_SERVE_CONFIG       --config path    (default ~/.coderouter/providers.yaml)
#   DEMO_SERVE_MODE         --mode value     (default coding; pass "" to omit)
#   DEMO_SERVE_PORT         --port value     (default 4000)
#   DEMO_SERVE_LOG          server log file  (default /tmp/coderouter-demo.log)
#   DEMO_SERVE_WAIT_S       healthz timeout  (default 30)
#
#   Progress readout:
#   DEMO_STATUS_EVERY       print an "elapsed + scenario counts" line
#                           every N ticks (default 10 -- ~ every 40s at the
#                           default 4s tick). Set to 0 to suppress.
#
# Exit codes:
#   0   completed normally (--duration expired, or Ctrl+C)
#   2   prerequisite failure (server unreachable, or --serve failed to
#       come up within DEMO_SERVE_WAIT_S)
#   64  bad argv (unknown flag, unparseable --duration)

set -u

# --- Arg parse ---------------------------------------------------------------
SERVE=0
DRY_RUN=0
DURATION_S=0   # 0 = run until Ctrl+C

# Parse a duration string -> integer seconds. Accepts "90", "30s", "5m",
# "1h". Returns 0 / echoes the integer on success, returns 1 / echoes an
# error on failure. Kept in a function so both --duration and future
# DEMO_DURATION env can share.
parse_duration() {
  local raw="$1"
  case "$raw" in
    ''|*[!0-9smhSMH]* )
      if ! [[ "$raw" =~ ^[0-9]+[smhSMH]?$ ]]; then
        echo "invalid duration: $raw (examples: 90, 30s, 5m, 1h)" >&2
        return 1
      fi
      ;;
  esac
  local num="${raw%[smhSMH]}"
  local unit="${raw#"$num"}"
  case "$unit" in
    ''|s|S) echo "$num" ;;
    m|M)    echo "$(( num * 60 ))" ;;
    h|H)    echo "$(( num * 3600 ))" ;;
    *)
      echo "invalid duration unit in: $raw" >&2
      return 1
      ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --serve) SERVE=1; shift ;;
    --dry-run|--dry) DRY_RUN=1; shift ;;
    --duration)
      shift
      if [ $# -eq 0 ]; then
        echo "--duration requires an argument (e.g. 5m)" >&2
        exit 64
      fi
      if ! DURATION_S="$(parse_duration "$1")"; then
        exit 64
      fi
      shift
      ;;
    --duration=*)
      if ! DURATION_S="$(parse_duration "${1#--duration=}")"; then
        exit 64
      fi
      shift
      ;;
    -h|--help)
      sed -n '2,/^set -u/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'
      exit 0
      ;;
    *)
      echo "unknown arg: $1 (try --help)" >&2
      exit 64
      ;;
  esac
done

# --- Serve knobs -------------------------------------------------------------
SERVE_BIN="${DEMO_SERVE_BIN:-uv run coderouter}"
SERVE_CONFIG="${DEMO_SERVE_CONFIG:-$HOME/.coderouter/providers.yaml}"
SERVE_MODE="${DEMO_SERVE_MODE-coding}"     # note: '-' so empty string means "omit"
SERVE_PORT="${DEMO_SERVE_PORT:-4000}"
SERVE_LOG="${DEMO_SERVE_LOG:-/tmp/coderouter-demo.log}"
SERVE_WAIT_S="${DEMO_SERVE_WAIT_S:-30}"

# When --serve is on, BASE_URL follows the port we chose; otherwise env wins.
if [ "$SERVE" = "1" ]; then
  BASE_URL="${CODEROUTER_URL:-http://127.0.0.1:${SERVE_PORT}}"
else
  BASE_URL="${CODEROUTER_URL:-http://127.0.0.1:4000}"
fi

DEFAULT_PROFILE="${DEMO_PROFILE:-free-only}"
FALLBACK_PROFILE="${DEMO_FALLBACK_PROFILE:-coding}"
PAID_PROFILE="${DEMO_PAID_PROFILE:-paid-only-demo}"
TICK_SLEEP="${DEMO_TICK_SLEEP:-4}"
BURST_SIZE="${DEMO_BURST_SIZE:-5}"
MAX_TOKENS="${DEMO_MAX_TOKENS:-48}"
STATUS_EVERY="${DEMO_STATUS_EVERY:-10}"

SERVER_PID=""
START_EPOCH=0
COUNT_NORMAL=0
COUNT_STREAM=0
COUNT_BURST_STEADY=0
COUNT_FALLBACK=0
COUNT_PAID_GATE=0

# ---------------------------------------------------------------------------
# Prompt pool -- small, coding-flavored, cheap to generate. Kept short so
# rate limits bite quickly (we WANT rate-limit 429s in the fallback
# scenario). Feel free to append your own lines.
# ---------------------------------------------------------------------------
PROMPTS=(
  "Write a Python one-liner that reverses a string."
  "Explain what async/await does in one short sentence."
  "Translate this Python to Go: print(sum(range(10)))."
  "What is the time complexity of binary search?"
  "Give me a bash snippet to count lines in every .py file under cwd."
  "Write a Rust match arm that handles Option<i32>."
  "What's the difference between a list and a tuple in Python?"
  "Show a regex that matches a v4 UUID."
  "Name three ways to deduplicate a list in Python."
  "Write a SQL query for the 3rd highest salary."
  "What does \`set -euo pipefail\` mean in bash?"
  "Give me a git command to undo the last commit but keep changes."
)

SYSTEM_MSG="You are a terse coding assistant. Keep answers under 25 words."

# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------
ts() { date +%H:%M:%S; }
log() {
  # $1=tag (color code), $2=scenario, $3=profile, $4=status
  local color="$1"; shift
  printf '\033[%sm[%s]\033[0m %-12s profile=%-18s %s\n' \
    "$color" "$(ts)" "$1" "$2" "$3" >&2
}
log_info()  { log "36" "$@"; }   # cyan
log_ok()    { log "32" "$@"; }   # green
log_warn()  { log "33" "$@"; }   # yellow
log_err()   { log "31" "$@"; }   # red

# Format an integer number of seconds as "Hh MMm SSs" / "Mm SSs" / "Ss".
# Defined here (near the other formatters) so the banner block below can
# reference it before the main-loop section that also uses it.
format_elapsed() {
  local secs="$1" h m s
  h=$(( secs / 3600 ))
  m=$(( (secs % 3600) / 60 ))
  s=$(( secs % 60 ))
  if (( h > 0 )); then
    printf '%dh %02dm %02ds' "$h" "$m" "$s"
  elif (( m > 0 )); then
    printf '%dm %02ds' "$m" "$s"
  else
    printf '%ds' "$s"
  fi
}

# ---------------------------------------------------------------------------
# Prereq -- server has to be reachable. --serve starts one for us; otherwise
# we expect the operator to have one running already. Fail fast with a
# friendly message in either case.
# ---------------------------------------------------------------------------

wait_for_healthz() {
  # Poll /healthz until 200 or timeout. Returns 0 on success, 1 on timeout.
  local timeout="$1" elapsed=0 step=1
  while (( elapsed < timeout )); do
    if curl -sS -f "$BASE_URL/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$step"
    elapsed=$((elapsed + step))
  done
  return 1
}

start_server() {
  # If something is already listening on $BASE_URL, don't start a second
  # one -- that would crash on "address in use" anyway, and the demo just
  # works against the existing server.
  if curl -sS -f "$BASE_URL/healthz" >/dev/null 2>&1; then
    echo "[demo_traffic] $BASE_URL is already live; reusing it (not launching a new server)" >&2
    return 0
  fi

  if [ ! -f "$SERVE_CONFIG" ]; then
    cat >&2 <<EOF
ERROR: --serve config not found at $SERVE_CONFIG

Either create the file (copy from examples/providers.yaml and edit) or
override with DEMO_SERVE_CONFIG=/abs/path ./scripts/demo_traffic.sh --serve
EOF
    return 2
  fi

  # Assemble the argv. SERVE_MODE is optional -- empty string means "don't
  # pass --mode" which lets the YAML's default_profile win.
  local -a cmd
  # shellcheck disable=SC2206  # splitting DEMO_SERVE_BIN on whitespace is intentional
  cmd=( $SERVE_BIN serve --config "$SERVE_CONFIG" --port "$SERVE_PORT" )
  if [ -n "$SERVE_MODE" ]; then
    cmd+=( --mode "$SERVE_MODE" )
  fi

  echo "[demo_traffic] starting: ${cmd[*]}" >&2
  echo "[demo_traffic] server log: $SERVE_LOG" >&2
  # Run from repo root so relative paths inside CodeRouter work. The
  # script might be invoked as ./scripts/demo_traffic.sh or via absolute
  # path; either way, the repo root is the parent of this script's dir.
  local script_dir repo_root
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  repo_root="$(cd "$script_dir/.." && pwd)"

  # Open the log with > (truncate) so each demo run starts clean.
  ( cd "$repo_root" && "${cmd[@]}" >"$SERVE_LOG" 2>&1 ) &
  SERVER_PID=$!
  echo "[demo_traffic] server pid: $SERVER_PID" >&2

  if ! wait_for_healthz "$SERVE_WAIT_S"; then
    cat >&2 <<EOF
ERROR: coderouter serve did not become healthy within ${SERVE_WAIT_S}s

Last 30 lines of $SERVE_LOG:
$(tail -n 30 "$SERVE_LOG" 2>/dev/null || echo '(log unreadable)')

The server process (pid $SERVER_PID) has been left running -- kill it
with:
  kill $SERVER_PID
EOF
    return 2
  fi
  echo "[demo_traffic] server healthy on $BASE_URL" >&2
}

stop_server() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[demo_traffic] stopping server (pid $SERVER_PID)..." >&2
    # Uvicorn handles SIGTERM cleanly.
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    # Give it up to 5s; SIGKILL as last resort.
    local waited=0
    while kill -0 "$SERVER_PID" 2>/dev/null && (( waited < 5 )); do
      sleep 1
      waited=$((waited + 1))
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "[demo_traffic] SIGTERM ignored after 5s; sending SIGKILL" >&2
      kill -KILL "$SERVER_PID" 2>/dev/null || true
    fi
  fi
}

if [ "$DRY_RUN" = "1" ]; then
  # Dry-run short-circuit: don't start anything, don't require the server
  # to be live. The plan block below is computed from argv/env so it's
  # meaningful either way.
  :
elif [ "$SERVE" = "1" ]; then
  if ! start_server; then
    exit 2
  fi
elif ! curl -sS -f "$BASE_URL/healthz" >/dev/null 2>&1; then
  cat >&2 <<EOF
ERROR: CodeRouter not reachable at $BASE_URL/healthz

Either start the server in another terminal:

    uv run coderouter serve \\
      --config ~/.coderouter/providers.yaml \\
      --mode coding \\
      --port 4000

...or re-run this script with --serve so it launches one for you:

    ./scripts/demo_traffic.sh --serve
EOF
  exit 2
fi

# Stash a JSON snapshot of /metrics.json so we can mention useful
# server-side knobs in the banner. Skipped in --dry-run (no server
# guaranteed to exist) so the banner renders instantly.
if [ "$DRY_RUN" = "1" ]; then
  ALLOW_PAID="?"
  DEFAULT_ON_SERVER="?"
  TZ_ON_SERVER="?"
else
  SNAPSHOT="$(curl -sS --max-time 3 "$BASE_URL/metrics.json" 2>/dev/null || true)"
  ALLOW_PAID="$(printf '%s' "$SNAPSHOT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["config"]["allow_paid"])' 2>/dev/null || echo "?")"
  DEFAULT_ON_SERVER="$(printf '%s' "$SNAPSHOT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["config"]["default_profile"])' 2>/dev/null || echo "?")"
  TZ_ON_SERVER="$(printf '%s' "$SNAPSHOT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["config"].get("display_timezone") or "UTC")' 2>/dev/null || echo "?")"
fi

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
if [ "$SERVE" = "1" ] && [ -n "$SERVER_PID" ]; then
  SERVE_LINE="  server launched:   pid=$SERVER_PID  log=$SERVE_LOG"
else
  SERVE_LINE="  server launched:   (no -- using externally-running instance)"
fi
if (( DURATION_S > 0 )); then
  DURATION_LINE="  duration:          $(format_elapsed "$DURATION_S")   (auto-stop)"
else
  DURATION_LINE="  duration:          unbounded (Ctrl+C to stop)"
fi
if (( STATUS_EVERY > 0 )); then
  STATUS_LINE="  status readout:    every $STATUS_EVERY ticks (~ every $((STATUS_EVERY * TICK_SLEEP))s)"
else
  STATUS_LINE="  status readout:    disabled (DEMO_STATUS_EVERY=0)"
fi

# --- Scenario catalog + expected-counts plan ---------------------------------
# Computed in one Python call so the fractions stay honest (bash integer
# math would round everything to 0). When --duration is 0 the "expected"
# columns are omitted (we don't know the total).
#
# The Python source is held in a single-quoted bash variable and passed
# to `python3 -c` instead of using a heredoc inside "$(...)". Legacy
# bash (e.g. macOS /bin/bash 3.2) has a long-standing parser bug where
# a heredoc nested inside command substitution can be mis-tokenized,
# producing confusing "syntax error near unexpected token" messages at
# unrelated line numbers further down the file.
PLAN_PY_SRC='
import os

dur    = int(os.environ["DUR_S"])
tick_s = int(os.environ["TICK"])
burst  = int(os.environ["BURST"])
prof   = os.environ["PROF"]
fb     = os.environ["FB"]
paid   = os.environ["PAID"]

# Effective share per scenario.
# Every 8th tick is a fixed paid-gate scenario; the other 7/8 of ticks
# run the weighted pick (4/3/2/1 out of 10).
paid_share       = 1/8
other            = 7/8
normal_share     = other * 4/10
stream_share     = other * 3/10
burst_idle_share = other * 2/10
fallback_share   = other * 1/10

# Requests per tick for each scenario.
reqs_per = {
    "normal":     1,
    "stream":     1,
    "burst+idle": 4,              # 3 concurrent + 1 steady
    "fallback":   burst,
    "paid-gate":  1,
}

rows = [
    ("normal",     normal_share,     reqs_per["normal"],     "{0} stream=false".format(prof)),
    ("stream",     stream_share,     reqs_per["stream"],     "{0} stream=true".format(prof)),
    ("burst+idle", burst_idle_share, reqs_per["burst+idle"], "{0} mixed".format(prof)),
    ("fallback",   fallback_share,   reqs_per["fallback"],   "{0} burst x{1} stream=true".format(fb, burst)),
    ("paid-gate",  paid_share,       reqs_per["paid-gate"],  "{0} stream=false".format(paid)),
]

have_dur = dur > 0
total_ticks = dur // tick_s if have_dur else None

def fmt_count(share, per):
    if not have_dur:
        return "--"
    ticks_for_scn = total_ticks * share
    reqs          = ticks_for_scn * per
    return "{0:5.1f}  {1:6.1f}".format(ticks_for_scn, reqs)

print("  Scenario catalog (what actually runs at each tick):")
header = "    name         share   req/tick  profile & flags"
if have_dur:
    header += "           exp.ticks  exp.reqs"
print(header)
print("    " + "-" * (60 if not have_dur else 82))
for name, share, per, desc in rows:
    left = "    {0:<11}  {1:4.1f}%   {2:>7}   {3:<34}".format(name, share*100, per, desc)
    if have_dur:
        left += "   " + fmt_count(share, per)
    print(left)

if have_dur:
    total_reqs = 0.0
    for name, share, per, _ in rows:
        total_reqs += total_ticks * share * per
    print("    total:                                                               "
          "  {0:5d}  {1:6.1f}".format(total_ticks, total_reqs))
'

PLAN_BLOCK="$(
  DUR_S="$DURATION_S" TICK="$TICK_SLEEP" BURST="$BURST_SIZE" \
  PROF="$DEFAULT_PROFILE" FB="$FALLBACK_PROFILE" PAID="$PAID_PROFILE" \
  python3 -c "$PLAN_PY_SRC"
)"

cat >&2 <<EOF
------------------------------------------------------------------------
CodeRouter demo traffic
  target:            $BASE_URL
$SERVE_LINE
  server default:    profile=$DEFAULT_ON_SERVER  allow_paid=$ALLOW_PAID  tz=$TZ_ON_SERVER
  demo profile:      $DEFAULT_PROFILE         (normal / stream / steady)
  fallback profile:  $FALLBACK_PROFILE        (burst to force cascades)
  paid-only profile: $PAID_PROFILE     (skipped if missing -- see NOTE)
  tick interval:     ${TICK_SLEEP}s   burst size: $BURST_SIZE   max_tokens: $MAX_TOKENS
$DURATION_LINE
$STATUS_LINE

$PLAN_BLOCK

Open http://localhost:${BASE_URL##*:}/dashboard in a browser, then
watch the panels fill in.
------------------------------------------------------------------------
EOF

if [ "$DRY_RUN" = "1" ]; then
  echo "[demo_traffic] --dry-run: banner printed, no traffic will be fired." >&2
  if [ "$SERVE" = "1" ] && [ -n "$SERVER_PID" ]; then
    echo "[demo_traffic] stopping the server we just launched..." >&2
    stop_server
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Pick a random element from a bash array.
# Usage:  pick_from "${PROMPTS[@]}"
pick_from() {
  local arr=("$@")
  printf '%s' "${arr[$((RANDOM % ${#arr[@]}))]}"
}

# Build a JSON body. args: stream (true|false), prompt
# We pick a throwaway model name -- CodeRouter rewrites the `model` field
# based on the selected chain, so this is purely cosmetic in logs.
#
# The prompt/system strings are passed via env vars (not interpolated
# into the Python source) so apostrophes and backslashes can't escape
# and break the shell -> Python handoff. ``stream`` is a plain true/false
# literal and safe to sub directly.
#
# Implementation note: the Python source lives in a single-quoted bash
# variable and runs via `python3 -c "$BODY_PY_SRC"`, NOT via a heredoc
# piped into `python3 -`. That matters because build_body is always
# called inside command substitution (body="$(build_body ...)"), and
# macOS /bin/bash 3.2 has intermittent fd-leak / hang behavior when a
# function using a heredoc is repeatedly invoked from $(...). Driving
# Python with `-c` sidesteps the issue entirely.
BODY_PY_SRC='
import json, os, sys
stream = os.environ["BODY_STREAM"].lower() == "true"
body = {
    "model": "demo",
    "stream": stream,
    "max_tokens": int(os.environ["BODY_MAX_TOKENS"]),
    "messages": [
        {"role": "system", "content": os.environ["BODY_SYSTEM"]},
        {"role": "user",   "content": os.environ["BODY_PROMPT"]},
    ],
}
sys.stdout.write(json.dumps(body))
'

build_body() {
  local stream="$1" prompt="$2"
  BODY_STREAM="$stream" BODY_PROMPT="$prompt" BODY_SYSTEM="$SYSTEM_MSG" \
  BODY_MAX_TOKENS="$MAX_TOKENS" \
  python3 -c "$BODY_PY_SRC"
}

# Fire ONE request.
# args: profile, stream (true|false), [bg=1 to background it]
# Writes "HTTP_CODE" on stdout (or "ERR" on transport failure).
fire_one() {
  local profile="$1" stream="$2" bg="${3:-0}"
  local prompt
  prompt="$(pick_from "${PROMPTS[@]}")"
  local body
  body="$(build_body "$stream" "$prompt")"
  if [ "$bg" = "1" ]; then
    (
      local code
      code="$(
        curl -sS -o /dev/null -w '%{http_code}' \
          -X POST "$BASE_URL/v1/chat/completions" \
          -H 'Content-Type: application/json' \
          -H "X-CodeRouter-Profile: $profile" \
          --max-time 60 \
          -d "$body" || echo ERR
      )"
      if [ "$code" = "200" ]; then
        log_ok  "burst-bg"  "$profile" "stream=$stream -> $code"
      else
        log_warn "burst-bg" "$profile" "stream=$stream -> $code"
      fi
    ) &
    return 0
  fi
  local code
  code="$(
    curl -sS -o /dev/null -w '%{http_code}' \
      -X POST "$BASE_URL/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -H "X-CodeRouter-Profile: $profile" \
      --max-time 60 \
      -d "$body" || echo ERR
  )"
  printf '%s' "$code"
}

# Wait for an explicit list of background PIDs.
#
# Background: bare `wait` with no arguments occasionally hangs on
# macOS /bin/bash 3.2 when the bg children themselves used command
# substitution (`$(curl ...)`) -- SIGCHLD delivery can be missed,
# leaving `wait` parked forever even though every child has exited.
# Waiting on explicit PIDs sidesteps the bug: a PID that has already
# exited simply returns its status immediately.
#
# Usage: fire the bg jobs collecting their $!, then `wait_pids "${pids[@]}"`.
wait_pids() {
  local pid
  for pid in "$@"; do
    # 2>/dev/null swallows "pid is not a child of this shell" if a
    # race already reaped it via another path.
    wait "$pid" 2>/dev/null
  done
}

# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

# Normal happy-path traffic, non-stream. The bread-and-butter case; most
# Providers and Requests/min movement comes from here.
scenario_normal() {
  COUNT_NORMAL=$((COUNT_NORMAL + 1))
  local code
  code="$(fire_one "$DEFAULT_PROFILE" false)"
  if [ "$code" = "200" ]; then
    log_ok   "normal"   "$DEFAULT_PROFILE" "stream=false -> $code"
  else
    log_warn "normal"   "$DEFAULT_PROFILE" "stream=false -> $code"
  fi
}

# Same happy-path, but stream=true so Recent Events lists a stream:true row.
# The dashboard's Recent Events column renders a dot for stream -- we want
# both colors visible.
scenario_stream() {
  COUNT_STREAM=$((COUNT_STREAM + 1))
  local code
  code="$(fire_one "$DEFAULT_PROFILE" true)"
  if [ "$code" = "200" ]; then
    log_ok   "stream"   "$DEFAULT_PROFILE" "stream=true  -> $code"
  else
    log_warn "stream"   "$DEFAULT_PROFILE" "stream=true  -> $code"
  fi
}

# BURST -> forces rate-limit 429 on the free-cloud tier, which in turn
# cascades the chain down to paid, which (with allow_paid=false) trips
# chain_paid_gate_blocked_total. Even if paid is allowed, the burst still
# bumps Fallback rate because at least one request in the burst will
# retry down the chain.
scenario_fallback_burst() {
  COUNT_FALLBACK=$((COUNT_FALLBACK + 1))
  log_info "fallback"  "$FALLBACK_PROFILE" "burst x $BURST_SIZE (forcing rate-limit cascade)"
  local i
  local pids=()
  for ((i=0; i<BURST_SIZE; i++)); do
    fire_one "$FALLBACK_PROFILE" true 1
    pids+=("$!")
  done
  wait_pids "${pids[@]}"
  log_info "fallback"  "$FALLBACK_PROFILE" "burst complete (all $BURST_SIZE returned)"
}

# Paid-gate block on first hop -- only works if DEMO_PAID_PROFILE exists on
# the server. We probe once; a 400 with "unknown profile" means we should
# silently fall back to the burst approach (which usually gets there via
# cascade anyway).
scenario_paid_gate_block() {
  COUNT_PAID_GATE=$((COUNT_PAID_GATE + 1))
  local body code
  body="$(build_body false 'probe')"
  code="$(
    curl -sS -o /dev/null -w '%{http_code}' \
      -X POST "$BASE_URL/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -H "X-CodeRouter-Profile: $PAID_PROFILE" \
      --max-time 10 \
      -d "$body" || echo ERR
  )"
  case "$code" in
    200|424|502|503)
      # 424/502/503: all providers exhausted -- the block counter fired.
      # 200: shouldn't happen if allow_paid=false + profile is paid-only,
      # but fine, we hit Providers regardless.
      log_ok   "paid-gate" "$PAID_PROFILE" "-> $code (paid-gate-block expected if allow_paid=false)"
      ;;
    400)
      log_warn "paid-gate" "$PAID_PROFILE" "-> 400 (profile missing -- falling back to burst)"
      scenario_fallback_burst
      ;;
    *)
      log_warn "paid-gate" "$PAID_PROFILE" "-> $code"
      ;;
  esac
}

# Short burst + 1 steady hit afterwards. Builds visible peaks on the
# Requests/min sparkline without starving the steady trickle needed for
# the "requests plateau" look.
scenario_burst_then_steady() {
  COUNT_BURST_STEADY=$((COUNT_BURST_STEADY + 1))
  log_info "burst+idle" "$DEFAULT_PROFILE" "3 concurrent + 1 steady"
  local pids=()
  fire_one "$DEFAULT_PROFILE" false 1; pids+=("$!")
  fire_one "$DEFAULT_PROFILE" true  1; pids+=("$!")
  fire_one "$DEFAULT_PROFILE" false 1; pids+=("$!")
  wait_pids "${pids[@]}"
  sleep 1
  # Diagnostic: if the steady-tail curl ever hangs, this log_info will be
  # the last line visible -- makes it unambiguous which call is stuck.
  log_info "burst+idle" "$DEFAULT_PROFILE" "firing steady tail (stream=true)"
  local code
  code="$(fire_one "$DEFAULT_PROFILE" true)"
  if [ "$code" = "200" ]; then
    log_ok   "burst+idle" "$DEFAULT_PROFILE" "steady tail stream=true -> $code"
  else
    log_warn "burst+idle" "$DEFAULT_PROFILE" "steady tail stream=true -> $code"
  fi
}

# ---------------------------------------------------------------------------
# Weighted scenario picker
#
# Weight table (sum to 10):
#   normal                  4   -- most ticks
#   stream                  3
#   burst_then_steady       2
#   fallback_burst          1   -- relatively expensive (rate-limit burn)
#   paid_gate_block is invoked on a fixed cadence (every 8 ticks) rather
#   than sharing the weights, because running it too often just thrashes.
# ---------------------------------------------------------------------------

pick_scenario() {
  local r=$((RANDOM % 10))
  if   [ "$r" -lt 4 ]; then echo scenario_normal
  elif [ "$r" -lt 7 ]; then echo scenario_stream
  elif [ "$r" -lt 9 ]; then echo scenario_burst_then_steady
  else                      echo scenario_fallback_burst
  fi
}

# ---------------------------------------------------------------------------
# Progress readout
# ---------------------------------------------------------------------------

# Print a one-line summary: elapsed / (optionally target) / tick / counts.
# $1 = current tick number. Always goes to stderr, in its own color so it
# reads as a "meta" line and not another request log.
print_status() {
  local tick="$1" now elapsed remaining_line="" target_line=""
  now=$(date +%s)
  elapsed=$(( now - START_EPOCH ))
  if (( DURATION_S > 0 )); then
    local remaining=$(( DURATION_S - elapsed ))
    (( remaining < 0 )) && remaining=0
    target_line="  target=$(format_elapsed "$DURATION_S")"
    remaining_line="  remaining=$(format_elapsed "$remaining")"
  fi
  # Bold magenta so it stands out from the per-request lines.
  printf '\033[1;35m[%s] -- status --\033[0m  tick=%d  elapsed=%s%s%s  ' \
    "$(ts)" "$tick" "$(format_elapsed "$elapsed")" "$target_line" "$remaining_line" >&2
  printf 'normal=%d stream=%d burst+idle=%d fallback=%d paid-gate=%d\n' \
    "$COUNT_NORMAL" "$COUNT_STREAM" "$COUNT_BURST_STEADY" \
    "$COUNT_FALLBACK" "$COUNT_PAID_GATE" >&2
}

# Called on normal exit (Ctrl+C or --duration expired). One last summary
# so you can copy-paste it into a retro / notes.
print_final_summary() {
  local now elapsed total
  now=$(date +%s)
  elapsed=$(( now - START_EPOCH ))
  total=$(( COUNT_NORMAL + COUNT_STREAM + COUNT_BURST_STEADY + COUNT_FALLBACK + COUNT_PAID_GATE ))
  printf '\n\033[1;35m---- final summary ----\033[0m\n' >&2
  printf '  elapsed:     %s\n' "$(format_elapsed "$elapsed")" >&2
  printf '  total ticks: %d\n' "$total" >&2
  printf '  breakdown:   normal=%d stream=%d burst+idle=%d fallback=%d paid-gate=%d\n' \
    "$COUNT_NORMAL" "$COUNT_STREAM" "$COUNT_BURST_STEADY" \
    "$COUNT_FALLBACK" "$COUNT_PAID_GATE" >&2
  if [ "$SERVE" = "1" ] && [ -n "$SERVER_PID" ]; then
    printf '  server log:  %s  (pid was %s)\n' "$SERVE_LOG" "$SERVER_PID" >&2
  fi
  printf '\n' >&2
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# Cleanup: wait for in-flight background curl requests to finish, print
# the final summary, then stop the server iff we launched it (--serve).
# If we didn't launch the server, leave the operator's own instance
# alone.
cleanup() {
  echo >&2
  log_info "exit" "-" "shutting down; waiting for bg requests..."
  # Only wait on children we launched (curl bg). 2> to swallow "no
  # children" if there are none.
  wait 2>/dev/null || true
  print_final_summary
  if [ "$SERVE" = "1" ]; then
    stop_server
  fi
  exit 0
}
trap cleanup INT TERM

START_EPOCH=$(date +%s)
tick=0
while true; do
  tick=$((tick + 1))
  if (( tick % 8 == 0 )); then
    scenario_paid_gate_block
  else
    scenario="$(pick_scenario)"
    "$scenario"
  fi

  # Periodic status readout: skip when STATUS_EVERY=0.
  if (( STATUS_EVERY > 0 )) && (( tick % STATUS_EVERY == 0 )); then
    print_status "$tick"
  fi

  # Duration cap: stop cleanly (same path as Ctrl+C) once elapsed >= cap.
  # Check BEFORE sleeping so we don't sit in the last sleep after the
  # budget has already run out.
  if (( DURATION_S > 0 )); then
    now=$(date +%s)
    if (( now - START_EPOCH >= DURATION_S )); then
      log_info "duration" "-" "reached --duration target ($(format_elapsed "$DURATION_S")), stopping"
      cleanup
    fi
  fi

  sleep "$TICK_SLEEP"
done

# ---------------------------------------------------------------------------
# NOTE -- paid-gate block demo
#
# The cleanest way to see chain_paid_gate_blocked_total move on every
# tick is to add a paid-only profile to your providers.yaml, e.g.:
#
#   profiles:
#     - name: paid-only-demo
#       providers:
#         - openrouter-claude    # or anthropic-direct
#
# With allow_paid: false, every request to this profile fires
# `chain-paid-gate-blocked` on the way in and the counter goes up by 1.
# Without this profile, the fallback scenario still gets there
# eventually -- free-cloud rate limits (~20/min on OpenRouter free tier)
# cascade the chain down to the paid tier, which gets blocked. The
# cascade path is slower and less reliable, so if paid-gate-block is
# what you want to demo live, add the profile above and re-run:
#   coderouter serve --config ~/.coderouter/providers.yaml --port 4000
# ---------------------------------------------------------------------------

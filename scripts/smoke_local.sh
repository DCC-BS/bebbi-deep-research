#!/usr/bin/env bash
# Local smoke test without Open WebUI: starts the service (SQLite) against the local evaluation stack and runs one
# quick focus job through the pipe (scripts/test_pipe.py). Secrets are read from the evaluation repo's .env at runtime.
#
#   EVAL_ENV=/path/to/deep-research-evaluation/.env scripts/smoke_local.sh [preset] ["question"]
# Needs the evaluation stack (LLM gateway on localhost:4000, Firecrawl on localhost:3002); EVAL_ENV is required.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${EVAL_ENV:?set EVAL_ENV to the .env of the evaluation repository (it holds METER_API_KEY); this script needs the evaluation stack}"
[ -f "$EVAL_ENV" ] || { echo "EVAL_ENV=$EVAL_ENV does not exist" >&2; exit 1; }
KEY=$(grep -E '^METER_API_KEY=' "$EVAL_ENV" | cut -d= -f2-)
PRESET="${1:-grossrat}"
QUESTION="${2:-Welche Vorstösse zum Thema Stadtklima gab es im Grossen Rat Basel-Stadt? Bitte schnell und kurz.}"
PORT="${PORT:-18089}"

export RA_TOKEN="smoke-$$" HMAC_SECRET="smoke-$$" IS_PROD=false PYDANTIC_AI_NO_BANNER=1 RA_DATABASE_URL="sqlite:////tmp/bdr-smoke-$$.db" RA_PORT="$PORT" RA_HOST=127.0.0.1
export RA_LLM_BASE_URL="http://localhost:4000/v1" RA_LLM_API_KEY="${KEY}:bebbi-dr-smoke"
export RA_SEARCH_URL="http://localhost:4000/serper/search" RA_SEARCH_KEY="${KEY}:bebbi-dr-smoke"
export RA_FIRECRAWL_URL="http://localhost:3002" RA_NIGHT_WINDOW="${RA_NIGHT_WINDOW:-00:00-23:59}"   # the pipe queues every job for the night
export RA_WORKERS=1 RA_DAY_WORKERS=1 RA_POLL_SECONDS=2

uv run bebbi-deep-research > "/tmp/bdr-smoke-$$.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; wait $PID 2>/dev/null || true; rm -f /tmp/bdr-smoke-$$.db' EXIT
for _ in $(seq 40); do curl -sf "http://127.0.0.1:$PORT/health/liveness" >/dev/null && break; sleep 0.5; done
curl -s "http://127.0.0.1:$PORT/health/readiness"; echo
uv run python scripts/test_pipe.py "$QUESTION" "$PRESET" --url "http://127.0.0.1:$PORT"
echo "--- service log tail"; tail -n 8 "/tmp/bdr-smoke-$$.log"

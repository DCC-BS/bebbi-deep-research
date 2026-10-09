#!/usr/bin/env bash
# Local smoke test without Open WebUI: starts the service (SQLite) against the local evaluation stack and runs one
# quick focus job through the pipe (scripts/test_pipe.py). Secrets are read from the evaluation repo's .env at runtime.
#
#   scripts/smoke_local.sh [preset] ["question"]
set -euo pipefail
cd "$(dirname "$0")/.."
EVAL_ENV="${EVAL_ENV:-/home/yanick/code/deep-research-evaluation/.env}"
KEY=$(grep -E '^METER_API_KEY=' "$EVAL_ENV" | cut -d= -f2-)
PRESET="${1:-grossrat}"
QUESTION="${2:-Welche Vorstösse zum Thema Stadtklima gab es im Grossen Rat Basel-Stadt? Bitte schnell und kurz.}"
PORT="${PORT:-18089}"

export RA_TOKEN="smoke-$$" RA_DATABASE_URL="sqlite:////tmp/bdr-smoke-$$.db" RA_PORT="$PORT" RA_HOST=127.0.0.1
export RA_LLM_BASE_URL="http://localhost:4000/v1" RA_LLM_API_KEY="${KEY}:bebbi-dr-smoke"
export RA_SEARCH_URL="http://localhost:4000/serper/search" RA_SEARCH_KEY="${KEY}:bebbi-dr-smoke"
export RA_FIRECRAWL_URL="http://localhost:3002" RA_NIGHT_WINDOW="${RA_NIGHT_WINDOW:-20:00-05:00}"
export RA_WORKERS=1 RA_DAY_WORKERS=1 RA_POLL_SECONDS=2

uv run bebbi-deep-research > "/tmp/bdr-smoke-$$.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; wait $PID 2>/dev/null || true; rm -f /tmp/bdr-smoke-$$.db' EXIT
for _ in $(seq 40); do curl -sf "http://127.0.0.1:$PORT/readyz" >/dev/null && break; sleep 0.5; done
curl -s "http://127.0.0.1:$PORT/readyz"; echo
uv run python scripts/test_pipe.py "$QUESTION" "$PRESET" --url "http://127.0.0.1:$PORT"
echo "--- service log tail"; tail -n 8 "/tmp/bdr-smoke-$$.log"

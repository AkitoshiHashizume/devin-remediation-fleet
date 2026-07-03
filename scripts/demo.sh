#!/usr/bin/env bash
# One-command keyless demo: starts the orchestrator in mock mode, replays the
# fixture events (success / policy rejection / escalation / duplicate), and
# leaves the dashboard running at http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."

PY=$(command -v python3 || command -v python)
[ -x .venv/bin/python ] && PY=.venv/bin/python

export MOCK_MODE=1
export GITHUB_WEBHOOK_SECRET="${GITHUB_WEBHOOK_SECRET:-change-me}"
# pin the repo the fixtures were recorded against, so a sourced .env
# (real GITHUB_REPO) can't break the mock's PR-URL matching
export GITHUB_REPO="your-org/superset"
export DB_PATH="data/demo.sqlite3"
export DISPATCH_INTERVAL_SECONDS=2
export POLL_INTERVAL_SECONDS=2

rm -f "$DB_PATH"
mkdir -p data

# BIND_HOST=0.0.0.0 is set by the docker demo profile so the mapped port is
# reachable from the host; local runs stay loopback-only.
"$PY" -m uvicorn app.main:app --host "${BIND_HOST:-127.0.0.1}" --port 8000 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

echo "waiting for orchestrator..."
for _ in $(seq 1 60); do
  "$PY" -c "import httpx; httpx.get('http://localhost:8000/metrics.json', timeout=2)" 2>/dev/null && break
  sleep 0.5
done

"$PY" scripts/replay_events.py

echo
echo "── mock fleet is processing; state transitions land within ~10s ──"
sleep 10
"$PY" -c "import httpx, json; print(json.dumps(httpx.get('http://localhost:8000/metrics.json', timeout=5).json(), indent=2))"
echo
echo "Dashboard: http://localhost:8000  (Ctrl-C to stop)"
wait $SERVER_PID

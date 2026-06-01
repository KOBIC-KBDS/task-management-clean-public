#!/bin/zsh
set -u

ROOT="${TASK_MANAGEMENT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE="${TASK_MANAGEMENT_STATE:-$ROOT/.task-management-live}"
LOG_DIR="$STATE/logs"
LOCK_DIR="$STATE/slack-live-loop.lock"
PID_FILE="$LOCK_DIR/pid"
CHILD_PID_FILE="$LOCK_DIR/child.pid"
PY="${TASK_MANAGEMENT_PYTHON:-$ROOT/.venv/bin/python}"
DASHBOARD="${TASK_MANAGEMENT_DASHBOARD_OUTPUT:-$ROOT/out/live/dashboard.html}"
LOG_FILE="$LOG_DIR/slack-live-loop.launchd.log"

mkdir -p "$LOG_DIR" "$(dirname "$DASHBOARD")"
exec >> "$LOG_FILE" 2>&1

timestamp() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(timestamp)] wrapper starting"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" > "$PID_FILE"
else
  old_pid=""
  if [ -f "$PID_FILE" ]; then old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"; fi
  if [ -n "$old_pid" ] && ! kill -0 "$old_pid" 2>/dev/null; then
    echo "[$(timestamp)] removing stale lock for pid=$old_pid"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || exit 75
    echo "$$" > "$PID_FILE"
  else
    echo "[$(timestamp)] another live loop is already running pid=$old_pid"
    exit 0
  fi
fi

cleanup() {
  exit_code=$?
  if [ -n "${child_pid:-}" ] && kill -0 "$child_pid" 2>/dev/null; then
    echo "[$(timestamp)] forwarding stop to child pid=$child_pid"
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  echo "[$(timestamp)] wrapper stopping status=$exit_code"
  rm -rf "$LOCK_DIR"
}
trap cleanup EXIT INT TERM HUP

cd "$ROOT" || exit 70
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

if [ -f "$ROOT/.env.local" ]; then
  set -a
  source "$ROOT/.env.local"
  set +a
fi

DASHBOARD_URL="${TASK_MANAGEMENT_DASHBOARD_URL:-http://127.0.0.1:8787/dashboard.html}"

echo "[$(timestamp)] running slack-doctor"
"$PY" -X utf8 -m task_management.cli --state "$STATE" slack-doctor --strict
doctor_status=$?
if [ "$doctor_status" -ne 0 ]; then
  echo "[$(timestamp)] slack-doctor failed status=$doctor_status"
  exit "$doctor_status"
fi

echo "[$(timestamp)] running startup backlog fast cycle"
"$PY" -X utf8 -m task_management.cli --state "$STATE" slack-fast-cycle --send --actor me --dashboard-output "$DASHBOARD" --home-dashboard-url "$DASHBOARD_URL"
fast_status=$?
if [ "$fast_status" -ne 0 ]; then
  echo "[$(timestamp)] startup fast cycle failed status=$fast_status"
  exit "$fast_status"
fi

echo "[$(timestamp)] running socket loop"
"$PY" -X utf8 -m task_management.cli --state "$STATE" slack-socket-loop --send --actor me --dashboard-output "$DASHBOARD" --home-dashboard-url "$DASHBOARD_URL" &
child_pid=$!
echo "$child_pid" > "$CHILD_PID_FILE"
wait "$child_pid"
loop_status=$?
echo "[$(timestamp)] socket loop exited status=$loop_status"
exit "$loop_status"

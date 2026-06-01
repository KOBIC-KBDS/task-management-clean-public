#!/bin/zsh
set -u

ROOT="${TASK_MANAGEMENT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE="${TASK_MANAGEMENT_STATE:-$ROOT/.task-management-live}"
DASHBOARD="${TASK_MANAGEMENT_DASHBOARD_OUTPUT:-$ROOT/out/live/dashboard.html}"
LOG_DIR="$STATE/logs"
SUPERVISOR_LOG="$LOG_DIR/slack-secretary-supervisor.log"
COMMAND_LOG="$LOG_DIR/slack-secretary-command.log"
PID_FILE="$STATE/slack-secretary-supervisor.pid"
STOP_FILE="$STATE/STOP"
LAST_MORNING_FILE="$STATE/slack-secretary-last-morning"
LAST_AFTERNOON_FILE="$STATE/slack-secretary-last-afternoon"
LAST_EOD_FILE="$STATE/slack-secretary-last-eod"
PY="${TASK_MANAGEMENT_PYTHON:-$ROOT/.venv/bin/python}"

mkdir -p "$STATE" "$LOG_DIR" "$(dirname "$DASHBOARD")"
cd "$ROOT" || exit 70

timestamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "[$(timestamp)] $*" >> "$SUPERVISOR_LOG"; }

run_cli() {
  local command_name="$1"
  shift
  local now
  now="$(date '+%Y-%m-%dT%H:%M:%S')"
  log "run $command_name now=$now"
  "$PY" -X utf8 -m task_management.cli --state "$STATE" "$command_name" --now "$now" --actor me "$@" --send >> "$COMMAND_LOG" 2>&1
  local exit_code=$?
  log "$command_name exit=$exit_code"
  return "$exit_code"
}

export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

if [ -f "$ROOT/.env.local" ]; then
  set -a
  source "$ROOT/.env.local"
  set +a
fi

echo "$$" > "$PID_FILE"
log "secretary supervisor started pid=$$ state=$STATE"
DASHBOARD_URL="${TASK_MANAGEMENT_DASHBOARD_URL:-http://127.0.0.1:8787/dashboard.html}"

while [ ! -f "$STOP_FILE" ]; do
  today="$(date '+%Y-%m-%d')"
  hour="$(date '+%H')"
  minute="$(date '+%M')"
  minutes=$((10#$hour * 60 + 10#$minute))

  last_morning=""
  if [ -f "$LAST_MORNING_FILE" ]; then last_morning="$(cat "$LAST_MORNING_FILE" 2>/dev/null || true)"; fi
  if [ "$minutes" -ge 480 ] && [ "$minutes" -lt 720 ] && [ "$last_morning" != "$today" ]; then
    if run_cli morning-briefing --dashboard-url "$DASHBOARD_URL"; then echo "$today" > "$LAST_MORNING_FILE"; fi
  fi

  last_afternoon=""
  if [ -f "$LAST_AFTERNOON_FILE" ]; then last_afternoon="$(cat "$LAST_AFTERNOON_FILE" 2>/dev/null || true)"; fi
  if [ "$minutes" -ge 780 ] && [ "$minutes" -lt 1020 ] && [ "$last_afternoon" != "$today" ]; then
    if run_cli afternoon-briefing --dashboard-url "$DASHBOARD_URL"; then echo "$today" > "$LAST_AFTERNOON_FILE"; fi
  fi

  last_eod=""
  if [ -f "$LAST_EOD_FILE" ]; then last_eod="$(cat "$LAST_EOD_FILE" 2>/dev/null || true)"; fi
  if [ "$minutes" -ge 1050 ] && [ "$minutes" -lt 1380 ] && [ "$last_eod" != "$today" ]; then
    if run_cli end-of-day-review; then echo "$today" > "$LAST_EOD_FILE"; fi
  fi

  sleep 60
done

log "secretary supervisor stopped"

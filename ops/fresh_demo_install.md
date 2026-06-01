# Fresh Demo Install

This guide describes how to install the clean repository into a brand-new Slack
workspace with a brand-new local task database.

## What belongs in Git

The clean repository should contain:

- source code,
- tests,
- setup and operations documents,
- `.env.example` with placeholder key names only,
- launchd/script templates.

The clean repository must not contain:

- Slack bot or app tokens,
- `.env` or `.env.local`,
- SQLite databases,
- JSONL event logs,
- Slack transcripts,
- generated dashboards,
- task-core data.

The runtime database is local deployment state. It is created automatically when
the CLI opens the configured `--state` directory for the first time.

## State directory model

Pick one state directory for each Slack workspace/app pair:

```text
.task-management-demo/
  task_management.sqlite3
  events.jsonl
  logs/
```

Recommended names:

- `.task-management-demo` for a temporary demo workspace,
- `.task-management-live` for a real private deployment,
- `.task-management-smoke` for fixture-only smoke tests.

The state directory is intentionally ignored by Git. Do not copy it into another
workspace unless you intentionally want to share the same task queue, Slack
message dedupe state, approvals, and audit history.

## 1. Clone and install

macOS / Linux:

```bash
git clone https://github.com/YOUR_ORG/task-management.git
cd task-management
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -X utf8 -m pytest -q
```

Windows PowerShell:

```powershell
git clone https://github.com/YOUR_ORG/task-management.git
cd task-management
python -m venv .venv
. .venv/Scripts/Activate.ps1
python -m pip install -e .
python -X utf8 -m pytest -q
```

## 2. Connect task-core externally

Do not copy task-core into this repository. Point the environment at an existing
task-core checkout:

macOS / Linux:

```bash
export TASK_CORE_PATH=/path/to/task-core
```

Windows PowerShell:

```powershell
$env:TASK_CORE_PATH = "C:\path\to\task-core"
```

## 3. Prepare a semantic backend

Choose one login-session backend. Both paths keep Slack sends, SQLite writes,
approvals, and task-core preview validation in the deterministic Python core.
Do not put API keys in `.env.local`.

### Option A: Codex CLI

```bash
codex login
```

Set the runtime mode:

macOS / Linux:

```bash
export TASK_MANAGEMENT_OPERATING_AGENT=codex
export TASK_MANAGEMENT_CODEX_MODEL=gpt-5.5
```

Windows PowerShell:

```powershell
$env:TASK_MANAGEMENT_OPERATING_AGENT = "codex"
$env:TASK_MANAGEMENT_CODEX_MODEL = "gpt-5.5"
```

### Option B: Claude Code CLI

Launch `claude` once on the host that will run the bot and complete the
interactive login if the CLI requests it. Then set:

macOS / Linux:

```bash
export TASK_MANAGEMENT_OPERATING_AGENT=claude
export TASK_MANAGEMENT_CLAUDE_MODEL=sonnet
export TASK_MANAGEMENT_CLAUDE_FALLBACK=0
```

Windows PowerShell:

```powershell
$env:TASK_MANAGEMENT_OPERATING_AGENT = "claude"
$env:TASK_MANAGEMENT_CLAUDE_MODEL = "sonnet"
$env:TASK_MANAGEMENT_CLAUDE_FALLBACK = "0"
```

The Claude adapter uses print mode with the local login session, `--output-format
json`, and the strict operating-agent JSON schema. See
[`claude_code_backend_handoff.md`](claude_code_backend_handoff.md) for detailed
validation and troubleshooting.

Keep `TASK_MANAGEMENT_CLAUDE_FALLBACK=0` for the first clean install so login or
schema problems fail visibly. Operators who prefer continuity after validation
can explicitly set it to `1` and monitor `agent.fallback.used` events.

## 4. Create a new Slack app

In the target Slack workspace:

1. Create a new Slack app from scratch.
2. Add bot token scopes for DM intake:
   - `chat:write`
   - `im:history`
   - `im:write`
3. Enable App Home / direct messages if the workspace UI requires it.
4. Install or reinstall the app to the workspace.
5. Copy the bot token (`xoxb-...`) into local environment only.

For Socket Mode:

1. Enable Socket Mode.
2. Create an app-level token (`xapp-...`) with `connections:write`.
3. In Event Subscriptions, subscribe the bot to `message.im`.
4. Save changes and reinstall the app if Slack asks.

For channel triage demos, later add channel message events and invite the bot to
specific demo channels. Start with DM mode first because it is the smallest
working path.

For a screenshot-oriented settings checklist, see
[`slack_app_settings_guide.md`](slack_app_settings_guide.md).

## 5. Create local configuration

Copy the example file locally and edit the values. The copied file is ignored by
Git.

```bash
cp .env.example .env.local
```

Minimum demo values:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_USER_ID=U...
TASK_MANAGEMENT_SLACK_ACTOR_ID=me
TASK_MANAGEMENT_OPERATING_AGENT=codex
TASK_MANAGEMENT_CODEX_MODEL=gpt-5.5
# Or use:
# TASK_MANAGEMENT_OPERATING_AGENT=claude
# TASK_MANAGEMENT_CLAUDE_MODEL=sonnet
# TASK_MANAGEMENT_CLAUDE_FALLBACK=0
TASK_MANAGEMENT_STATE=.task-management-demo
TASK_MANAGEMENT_DASHBOARD_URL=http://127.0.0.1:8787/demo-dashboard.html
```

If you already know the app DM channel, also set:

```bash
SLACK_DM_CHANNEL_ID=D...
```

If not, the doctor command in the next step can resolve it with
`conversations.open`.

For macOS/Linux direct CLI commands, load `.env.local` into the current shell:

```bash
set -a
source .env.local
set +a
```

The provided wrapper scripts also load `.env.local` automatically. Windows
PowerShell users can instead set the same variables with `$env:...` commands in
the current session.

## 6. Create the empty demo database

Choose a fresh state directory:

```bash
export TASK_MANAGEMENT_STATE=.task-management-demo
```

Run the doctor. This validates configuration and creates the local state files.
With `--live-open-dm`, it may call Slack to resolve the personal DM channel, but
it does not send task messages.

```bash
python -X utf8 -m task_management.cli --state "$TASK_MANAGEMENT_STATE" slack-doctor --live-open-dm
```

After the first command, the local files should exist:

```bash
find "$TASK_MANAGEMENT_STATE" -maxdepth 2 -type f | sort
```

Expected key files:

```text
.task-management-demo/events.jsonl
.task-management-demo/task_management.sqlite3
```

Optional SQLite check:

```bash
sqlite3 "$TASK_MANAGEMENT_STATE/task_management.sqlite3" ".tables"
```

The database is empty except for schema/integration state until messages are
processed.

## 7. Validate Socket Mode

```bash
python -X utf8 -m task_management.cli --state "$TASK_MANAGEMENT_STATE" slack-socket-doctor
```

If this fails, fix Slack app scopes, Socket Mode token, event subscriptions, or
reinstall state before running a live loop.

## 8. Run a credential-free smoke test

This uses a local fixture and does not contact Slack:

```bash
python -X utf8 -m task_management.cli --agent rule --state .task-management-smoke \
  slack-socket-loop \
  --transcript-input examples/slack_socket_message_im.json \
  --dashboard-output out/smoke-dashboard.html \
  --max-events 1
```

Open the smoke dashboard if desired:

```bash
python -m http.server 8787 -d out
```

Then browse to:

```text
http://127.0.0.1:8787/smoke-dashboard.html
```

### Claude backend semantic smoke

If you selected Claude Code, run one local task-management intake through the
actual Claude CLI before touching Slack. This fixture sends one Korean private
task request to the orchestrator and should record `source="claude_code_cli"` in
the audit events.

```bash
rm -rf .task-management-claude-smoke
TASK_MANAGEMENT_OPERATING_AGENT=claude \
python -X utf8 -m task_management.cli --agent claude --state .task-management-claude-smoke \
  simulate --fixture examples/claude_smoke_private.json

python -X utf8 -m task_management.cli --state .task-management-claude-smoke events | grep '"source": "claude_code_cli"'
```

Expected behavior:

1. Claude Code print mode returns a strict decision envelope.
2. The deterministic core creates an approved task or a clarification request.
3. `events` contains `agent.decision.created` with `source="claude_code_cli"`.
4. The `.task-management-claude-smoke/` state directory remains local and ignored by Git.

## 9. Run the first live demo cycle

Send a simple DM to the Slack app, then run one polling cycle:

```bash
python -X utf8 -m task_management.cli --agent "$TASK_MANAGEMENT_OPERATING_AGENT" --state "$TASK_MANAGEMENT_STATE" \
  slack-fast-cycle \
  --dashboard-output out/demo-dashboard.html \
  --send
```

Expected behavior:

1. The message is recorded in SQLite.
2. The configured semantic backend interprets it into a strict decision envelope.
3. Deterministic policy creates a proposal or asks for missing information.
4. Slack receives the reply if `--send` is present.
5. `out/demo-dashboard.html` is updated.

## 10. Run the continuous Socket Mode loop

After the one-cycle demo works:

```bash
python -X utf8 -m task_management.cli --agent "$TASK_MANAGEMENT_OPERATING_AGENT" --state "$TASK_MANAGEMENT_STATE" \
  slack-socket-loop \
  --dashboard-output out/demo-dashboard.html \
  --home-dashboard-url http://127.0.0.1:8787/demo-dashboard.html \
  --send
```

Serve the dashboard from another terminal:

```bash
python -m http.server 8787 -d out
```

## 11. Optional proactive briefing loop

The secretary supervisor sends morning, afternoon, and end-of-day messages using
the same state directory. Run it only after DM intake is healthy.

```bash
TASK_MANAGEMENT_STATE=.task-management-demo \
TASK_MANAGEMENT_DASHBOARD_OUTPUT=out/demo-dashboard.html \
TASK_MANAGEMENT_DASHBOARD_URL=http://127.0.0.1:8787/demo-dashboard.html \
ops/slack-secretary-supervisor.sh
```

The loop writes logs under:

```text
.task-management-demo/logs/
```

Stop it with Ctrl-C in a foreground terminal or create:

```text
.task-management-demo/STOP
```

## 12. Reset or archive a demo

Stop all live loops first. Then archive the local state directory:

```bash
mv .task-management-demo ".task-management-demo.archive.$(date +%Y%m%d%H%M%S)"
```

The next run with `--state .task-management-demo` will create a new empty
database.

## Troubleshooting

- No database appears: confirm the CLI command includes the intended `--state`
  path and that the process has write permission in the repository directory.
- Doctor cannot open the DM: confirm `SLACK_USER_ID` is a human `U...` id, not
  a bot id, or manually set `SLACK_DM_CHANNEL_ID=D...`.
- Socket Mode connects but no messages arrive: confirm `message.im` event
  subscription and reinstall the Slack app after changes.
- Replies are not sent: confirm `--send` is present and `chat:write` is granted.
- Semantic decisions fall back to rule behavior: confirm the chosen CLI
  (`codex` or `claude`) is logged in and on `PATH`, then inspect `events.jsonl`
  for the agent error.
- Dashboard does not update: confirm `--dashboard-output` points to the file you
  are serving and refresh the browser.
- State got polluted during a demo: archive the state directory and start a new
  one instead of editing SQLite by hand.

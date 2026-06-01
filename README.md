# Example Lab Task Management

Private Slack-first task-management MVP for Example Lab team workflows.

The system turns natural Korean Slack messages into structured task/event/routine proposals, applies deterministic safety policy, persists local state, renders a web dashboard, and builds preview-only `task-core.export.v1` payloads.

## Product Principle

The operating agent is the semantic engine. It should read each message with active task context, decide whether the message creates a new item or updates an existing one, and return a strict JSON decision envelope. Deterministic code validates and commits the decision safely.

```text
Slack app message + active local task context
  -> Codex or Claude Code semantic operating-agent decision
  -> deterministic task/state policy
  -> Slack reply + web dashboard + task-core preview payload
```

Rules/keyword parsing is a fallback and test aid, not the product core.

## Deployment model

Slack is only the chat channel. It is not the owner of the task-management system.
The owner is this project runtime:

- local SQLite/JSONL state,
- deterministic orchestration and approval policy,
- task-core preview bridge,
- and the semantic operating agent, either `codex exec` with a local Codex login
  session or Claude Code print mode with a local Claude Code login session.

That means each private deployment can attach its own Slack app/bot account to the same clean repository:

1. Clone this repository.
2. Point `TASK_CORE_PATH` at that user's checked-out task-core repo.
3. Log in locally to the chosen semantic CLI (`codex login` or interactive
   `claude` login). No OpenAI/Anthropic API key is required for the login-session
   modes.
4. Create/install a private Slack app in the target workspace.
5. Set local Slack token/channel environment variables.
6. Run the same `slack-fast-cycle` or `slack-socket-loop` command.

Different teams or individuals may therefore run isolated deployments with different Slack apps, state directories, and semantic CLI login sessions while sharing the same source code. Do not commit any deployment-specific token, Slack transcript, dashboard output, SQLite DB, or JSONL log.

## Scope

- Private team deployment only.
- Slack personal DM / Slack app interaction first.
- Local SQLite state plus JSONL audit events.
- Optional Slack Socket Mode for low-latency message intake.
- Preview-only task-core export validation.
- No task-core inbox/raw/wiki writes.
- No tokens or live state committed to git.

## Setup

```powershell
python -m venv .venv
. .venv/Scripts/Activate.ps1
python -m pip install -e .
$env:TASK_CORE_PATH = "C:\path\to\llm-wiki"
python -X utf8 -m pytest -q
```

`task-core` remains external. For local development, keep `TASK_CORE_PATH` pointed at a checked-out task-core repo. Do not copy task-core into this repository.

## Slack configuration

Create a private Slack app and store tokens in your local environment, never in git.

For a complete new-operator walkthrough, see
[`ops/slack_bot_connection_guide.md`](ops/slack_bot_connection_guide.md).
For screenshot-driven Slack app settings, see
[`ops/slack_app_settings_guide.md`](ops/slack_app_settings_guide.md).
For a fresh demo workspace with a new empty local database, see
[`ops/fresh_demo_install.md`](ops/fresh_demo_install.md).
For Claude Code backend setup, see
[`ops/claude_code_backend_handoff.md`](ops/claude_code_backend_handoff.md).

Required bot scopes for DM mode:

- `chat:write`
- `im:history`
- `im:write`

Socket Mode additionally needs an app-level token (`xapp-...`) and subscribed events such as `message.im`.

Example local environment:

```powershell
$env:SLACK_BOT_TOKEN = "xoxb-..."
$env:SLACK_APP_TOKEN = "xapp-..."
$env:SLACK_USER_ID = "U..."
$env:SLACK_DM_CHANNEL_ID = "D..."
$env:TASK_MANAGEMENT_SLACK_ACTOR_ID = "me"
$env:TASK_MANAGEMENT_OPERATING_AGENT = "codex"
$env:TASK_MANAGEMENT_CODEX_MODEL = "gpt-5.5"
# Or:
# $env:TASK_MANAGEMENT_OPERATING_AGENT = "claude"
# $env:TASK_MANAGEMENT_CLAUDE_MODEL = "sonnet"
# $env:TASK_MANAGEMENT_CLAUDE_FALLBACK = "0"
```

Use doctor commands before live sends:

```powershell
python -X utf8 -m task_management.cli --state .task-management-live slack-doctor
python -X utf8 -m task_management.cli --state .task-management-live slack-socket-doctor
```

### Fresh demo database

Do not commit or share a demo database. A clean checkout contains source code,
sample configuration, and setup docs only. The runtime creates local state on
first use under the `--state` directory you choose:

```text
.task-management-demo/
  task_management.sqlite3
  events.jsonl
  logs/
```

Use one state directory per Slack workspace/app pair. For example, use
`.task-management-demo` for a temporary demo workspace and `.task-management-live`
for a real private deployment. To reset a demo, stop the live loops and archive
or delete that local state directory; the next command will create a fresh empty
SQLite database again.

## Runtime examples

Fast DM cycle:

```powershell
python -X utf8 -m task_management.cli --agent codex --state .task-management-live slack-fast-cycle `
  --dashboard-output out/dashboard.html `
  --send
```

Socket Mode loop:

```powershell
python -X utf8 -m task_management.cli --agent codex --state .task-management-live slack-socket-loop `
  --dashboard-output out/dashboard.html `
  --send
```


Proactive secretary checks:

```powershell
python -X utf8 -m task_management.cli --state .task-management-live morning-briefing `
  --now 2026-05-20T08:00:00 `
  --dashboard-url http://127.0.0.1:8787/dashboard.html

python -X utf8 -m task_management.cli --state .task-management-live proactive-checks `
  --now 2026-05-20T15:00:00

python -X utf8 -m task_management.cli --state .task-management-live end-of-day-review `
  --now 2026-05-20T21:00:00
```

The end-of-day review asks the operator to complete, update progress, or defer
open work due today before it silently rolls over. Completed items are ignored
for follow-up pressure. Natural replies such as `출장 준비물 다 쌌어`, `발표자료는 반쯤 했고 검토만 남음`, or
`ProjectA 정리는 금요일 오후로 미뤄줘` are handled by the semantic update path.

Reversible live E2E harness:

```powershell
python -X utf8 -m task_management.cli --state .task-management-live slack-e2e-clean `
  --run-id smoke-20260520 `
  --dashboard-output out/dashboard.html `
  --send

python -X utf8 -m task_management.cli --agent codex --state .task-management-live slack-e2e-run `
  --run-id smoke-20260520 `
  --dashboard-output out/dashboard.html `
  --send
```

`slack-e2e-clean` checkpoints local state before removing test messages/state;
the rollback checkpoint only restores test state and does not roll back source
code changes.

Replace `--agent codex` with `--agent claude` when the deployment uses Claude
Code as the semantic backend.

Correction workflow for bad bot replies:

```powershell
python -X utf8 -m task_management.cli --state .task-management-live slack-correct `
  --delete-ts 1234567890.123456 `
  --text-file correction.md `
  --verify-contains "반영했습니다" `
  --send
```

For Korean live/state side effects, always use UTF-8 files or scripts. Do not pipe Korean correction text through PowerShell stdin.

## Dashboard

```powershell
python -X utf8 -m task_management.cli --state .task-management-live render-dashboard --output out/dashboard.html
python -m http.server 8787 -d out
```

Open `http://127.0.0.1:8787/dashboard.html`.

## Verification

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -X utf8 -m pytest -q
```

## Repository hygiene

Do not commit:

- `.task-management-*` / local SQLite state
- `out/`
- Slack tokens or app tokens
- personal/team private transcripts that have not been anonymized
- task-core files

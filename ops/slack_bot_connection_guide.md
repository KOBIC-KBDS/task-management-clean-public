# Slack Bot Connection Guide

This guide is for a new private operator who wants to run this repository as
their own task-management assistant.

## Mental model

Slack is only the front door. The task-management system itself is:

- this repository's Python runtime,
- one local state directory (`--state ...`) with SQLite + JSONL audit logs,
- `task-core` referenced externally through `TASK_CORE_PATH`,
- and the semantic operating agent, either Codex CLI or Claude Code CLI using
  the operator's local login session.

Therefore each person or team can run the same clean source code with their own
Slack app, Slack tokens, state directory, task-core checkout, and semantic CLI
login.
Do not share tokens or state unless you intentionally want the same queue.

For a step-by-step clean demo workspace setup, including empty database
creation and reset, see [`fresh_demo_install.md`](fresh_demo_install.md).
For a screen-by-screen Slack app settings checklist, see
[`slack_app_settings_guide.md`](slack_app_settings_guide.md).

## Prerequisites

- Python 3.12+
- Git
- One semantic CLI installed and authenticated:
  - Codex CLI with `codex login`, or
  - Claude Code CLI with its interactive login flow
- A local checkout of task-core
- Permission to create/install a private Slack app in the target workspace

No OpenAI/Anthropic API key is needed for the login-session paths. The project
invokes the local CLI and relies on the authenticated user session.

## 1. Clone and install

```powershell
git clone https://github.com/YOUR_ORG/task-management.git
cd task-management

python -m venv .venv
. .venv/Scripts/Activate.ps1
python -m pip install -e .
```

Point the project at task-core:

```powershell
$env:TASK_CORE_PATH = "C:\path\to\llm-wiki"
```

Verify the clean checkout:

```powershell
python -X utf8 -m pytest -q
```

## 2. Configure the semantic engine

Choose exactly one backend for the runtime host.

### Option A: Codex CLI

Log in on the machine that will run the task bot:

```powershell
codex login
```

Set the operating-agent mode:

```powershell
$env:TASK_MANAGEMENT_OPERATING_AGENT = "codex"
$env:TASK_MANAGEMENT_CODEX_MODEL = "gpt-5.5"
```

The runtime calls Codex in read-only/no-approval mode for interpretation. Codex
may propose strict JSON decisions only; deterministic code remains responsible
for persistence, approvals, Slack sends, and task-core preview validation.

### Option B: Claude Code CLI

Launch `claude` on the machine that will run the task bot and complete the
interactive login if needed. Then set:

```powershell
$env:TASK_MANAGEMENT_OPERATING_AGENT = "claude"
$env:TASK_MANAGEMENT_CLAUDE_MODEL = "sonnet"
$env:TASK_MANAGEMENT_CLAUDE_FALLBACK = "0"
```

The runtime calls Claude Code print mode with a strict JSON schema. Claude may
propose strict JSON decisions only; deterministic code remains responsible for
persistence, approvals, Slack sends, and task-core preview validation.

## 3. Create the Slack app

1. Open <https://api.slack.com/apps>.
2. Create a new app from scratch in the target workspace.
3. In **OAuth & Permissions**, add bot token scopes:
   - `chat:write`
   - `im:history`
   - `im:write`
4. Install or reinstall the app to the workspace after changing scopes.
5. Copy the bot token (`xoxb-...`) to your local environment only.

For Socket Mode:

1. In **Basic Information** or **Socket Mode**, enable Socket Mode.
2. Generate an app-level token (`xapp-...`) with:
   - `connections:write`
3. In **Event Subscriptions**, Socket Mode means no Request URL is needed.
4. Subscribe the bot to:
   - `message.im`
5. Save changes and reinstall if Slack asks for it.

For user-to-bot DM entry:

1. Ensure the app has a bot user.
2. In **App Home**, enable the messages tab / allow users to send messages to
   the app if your workspace requires that switch.
3. Open the app in Slack and send it a test DM.

Optional Canvas publishing requires an additional token/scope path such as
`canvases:write`; keep that separate from the first DM intake test.

## 4. Create local Slack configuration

```powershell
Copy-Item .env.example .env.local
Copy-Item ops/local_env_markdown_template.md .env.local.md
```

Paste values from the new Slack app into `.env.local.md`. The recommended
minimum rows are:

```text
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_USER_ID=
SLACK_DM_CHANNEL_ID=
TASK_MANAGEMENT_INSTANCE_ID=clean-demo
TASK_MANAGEMENT_ALLOWED_INSTANCE_ID=clean-demo
```

The CLI automatically loads `.env.local` and then `.env.local.md` from the
current working directory. This intentionally overrides inherited shell
variables, including blank values, so a clean installation does not accidentally
reuse Slack tokens from another machine.

Choose an isolated state directory for this workspace. The first command that
opens the store creates the SQLite database and JSONL audit log automatically:

```powershell
$env:TASK_MANAGEMENT_STATE = ".task-management-demo"
python -X utf8 -m task_management.cli --state $env:TASK_MANAGEMENT_STATE slack-doctor
```

Expected local files after first run:

```text
.task-management-demo/task_management.sqlite3
.task-management-demo/events.jsonl
```

Never commit this state directory. Use a different state directory for each
Slack workspace/app pair.

If you do not know the `D...` DM channel id, let the doctor resolve it:

```powershell
python -X utf8 -m task_management.cli --state $env:TASK_MANAGEMENT_STATE slack-doctor --live-open-dm
```

Then pin it for future runs:

```powershell
$env:SLACK_DM_CHANNEL_ID = "D..."
```

## 5. Validate without sending

Run configuration checks:

```powershell
python -X utf8 -m task_management.cli --state $env:TASK_MANAGEMENT_STATE slack-doctor
python -X utf8 -m task_management.cli --state $env:TASK_MANAGEMENT_STATE slack-socket-doctor
```

Run a credential-free fixture through the Socket Mode path:

```powershell
python -X utf8 -m task_management.cli --agent rule --state .task-management-smoke `
  slack-socket-loop `
  --transcript-input examples/slack_socket_message_im.json `
  --dashboard-output out/smoke-dashboard.html `
  --max-events 1
```

If this works, the local project path is healthy. Live failures after this point
are usually Slack app permissions, token, event subscription, or DM channel
configuration issues.

## 6. Run live DM intake

Polling one fast cycle:

```powershell
python -X utf8 -m task_management.cli --agent $env:TASK_MANAGEMENT_OPERATING_AGENT --state $env:TASK_MANAGEMENT_STATE `
  slack-fast-cycle `
  --dashboard-output out/dashboard.html `
  --send
```

Socket Mode event loop:

```powershell
python -X utf8 -m task_management.cli --agent $env:TASK_MANAGEMENT_OPERATING_AGENT --state $env:TASK_MANAGEMENT_STATE `
  slack-socket-loop `
  --dashboard-output out/dashboard.html `
  --send
```

Open the dashboard:

```powershell
python -m http.server 8787 -d out
```

Then browse to <http://127.0.0.1:8787/dashboard.html>.

## Troubleshooting checklist

- Bot cannot receive DMs: enable App Home messages tab / app DM setting, then
  reinstall the app if Slack requires it.
- `slack-doctor` says token is wrong kind: `SLACK_BOT_TOKEN` must be `xoxb-...`;
  `SLACK_APP_TOKEN` must be `xapp-...`.
- Socket Mode connects but no messages arrive: confirm bot event subscription
  includes `message.im` and the app is installed after the change.
- `conversations.open` cannot resolve a DM: set `SLACK_USER_ID` to the human
  `U...` id or set `SLACK_DM_CHANNEL_ID=D...` manually.
- Semantic path falls back to rules: confirm the chosen CLI (`codex` or
  `claude`) is logged in and on `PATH`, then inspect the error text in the
  `agent.decision.created` or `agent.fallback.used` event.
- Korean text is garbled: use UTF-8 files/scripts (`python -X utf8`,
  `--text-file`) and avoid piping Korean text through PowerShell native stdin.

## Official references

- Slack tokens: <https://docs.slack.dev/authentication/tokens>
- Slack Socket Mode: <https://docs.slack.dev/apis/events-api/using-socket-mode>
- Slack Events API event subscriptions: <https://docs.slack.dev/apis/events-api>
- Slack App Home / messages tab: <https://docs.slack.dev/surfaces/app-home>

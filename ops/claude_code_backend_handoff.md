# Claude Code Backend Guide

This repository supports Claude Code as a first-class semantic operating-agent
backend for clean Slack deployments.

## What Claude Code owns

Claude Code only interprets an incoming message plus active task context and
returns one strict `task-task_management.operating-agent.v1` JSON decision.

It must not:

- write SQLite or JSONL state directly,
- approve proposals by itself,
- send Slack messages directly,
- write task-core inbox/raw/wiki files,
- require or print API keys.

The deterministic Python core still owns persistence, approval/missing-slot
policy, Slack sends, dashboard rendering, and preview-only task-core export.

## Runtime configuration

Install and authenticate the Claude Code CLI on the host that will run the bot.
If print mode says `Not logged in`, launch `claude` interactively and complete
the login flow.

Recommended environment:

```bash
TASK_MANAGEMENT_OPERATING_AGENT=claude
TASK_MANAGEMENT_CLAUDE_BIN=claude
TASK_MANAGEMENT_CLAUDE_MODEL=sonnet
TASK_MANAGEMENT_CLAUDE_EFFORT=
TASK_MANAGEMENT_CLAUDE_TIMEOUT_SECONDS=180
TASK_MANAGEMENT_CLAUDE_FALLBACK=0
TASK_MANAGEMENT_CLAUDE_PERMISSION_MODE=plan
TASK_MANAGEMENT_CLAUDE_MAX_TURNS=3
TASK_MANAGEMENT_CLAUDE_NO_SESSION_PERSISTENCE=1
TASK_MANAGEMENT_CLAUDE_TOOLS=
TASK_MANAGEMENT_CLAUDE_STRIP_API_KEY_ENV=1
```

`TASK_MANAGEMENT_CLAUDE_MODEL` accepts an alias (`sonnet`/`opus`) that resolves to
the latest model. `TASK_MANAGEMENT_CLAUDE_EFFORT` is optional: `low`/`medium`/`high`/
`xhigh`/`max` passes `--effort` to print mode and is an effective latency lever
(measured ~21s at `low` vs ~73s unset on the smoke fixture). The Codex backend has
`TASK_MANAGEMENT_CODEX_EFFORT` → `model_reasoning_effort`, which tunes reasoning DEPTH
for `codex exec` (reasoning tokens scale strongly) rather than wall-clock latency.

The same Slack app, state-directory, and task-core setup can be reused with
`TASK_MANAGEMENT_OPERATING_AGENT=codex` if you choose Codex instead.

## CLI behavior

The adapter calls Claude Code print mode with:

```text
claude -p --no-session-persistence --permission-mode plan --tools "" \
  --max-turns 3 --output-format json --json-schema <strict schema>
```

With Claude Code 2.1.159, successful print mode returns a JSON envelope. The
adapter:

1. parses stdout as the Claude envelope,
2. rejects non-zero exit or `is_error=true`,
3. prefers the `structured_output` object,
4. otherwise extracts a JSON object from the `result` string,
5. validates it through `decision_from_payload(...)`,
6. records `source="claude_code_cli"` on success.

By default the Claude adapter fails closed (`TASK_MANAGEMENT_CLAUDE_FALLBACK=0`)
so a login/session/schema problem is visible during clean install and live
validation. If an operator explicitly prefers continuity over backend integrity,
set `TASK_MANAGEMENT_CLAUDE_FALLBACK=1`; invalid/unavailable Claude output then
falls back to the deterministic rule agent and records
`source="claude_code_cli_fallback"` for observability.

## Smoke test without Slack

Run a local fixture through the Claude semantic adapter:

```bash
TASK_MANAGEMENT_OPERATING_AGENT=claude \
python -X utf8 -m task_management.cli --agent claude --state .task-management-claude-smoke \
  simulate --fixture examples/claude_smoke_private.json
```

Then inspect:

```bash
python -X utf8 -m task_management.cli --state .task-management-claude-smoke events
```

Expected evidence:

- at least one `agent.decision.created` event,
- decision source is `claude_code_cli` for a healthy Claude path,
- or `claude_code_cli_fallback` with an error rationale if fallback was used,
- no runtime state files are staged by Git.

## Live Slack demo

After Slack doctor checks pass and a demo DM has been sent to the app:

```bash
TASK_MANAGEMENT_OPERATING_AGENT=claude \
python -X utf8 -m task_management.cli --agent claude --state "$TASK_MANAGEMENT_STATE" \
  slack-fast-cycle \
  --dashboard-output out/demo-dashboard.html \
  --send
```

Expected behavior:

1. The message is recorded in local SQLite.
2. Claude Code emits a strict decision envelope.
3. The deterministic core creates a proposal, applies feedback, or asks a
   clarification.
4. Slack receives the rendered response if `--send` is present.
5. The dashboard file is updated.

For continuous operation, use the same `--agent claude` value with
`slack-socket-loop`.

## Troubleshooting

- `invalid choice: 'claude'`: install a version of this repo that includes
  `task_management/claude_code_operating_agent.py` and the CLI wiring.
- `Not logged in`: launch `claude` interactively on the same host/user account
  and complete login.
- Fallback source appears in events: inspect the fallback rationale in
  `events.jsonl`; common causes are missing CLI, login expiry, unsupported
  flags, timeout, or schema-invalid model output.
- The bot runs but does not send Slack messages: verify Slack tokens/scopes and
  that the live command includes `--send`.
- Korean text is garbled: use UTF-8 files/scripts and `python -X utf8`; do not
  pipe Korean live-correction text through native shell stdin.

## Verification for maintainers

```bash
python -X utf8 -m pytest -q tests/test_claude_code_operating_agent.py
python -X utf8 -m pytest -q
git diff --check
```

Before committing, confirm that `.env`, state directories, SQLite databases,
JSONL event logs, generated dashboards, Slack transcripts, and tokens are not
staged.

## References

- Claude Code CLI reference: <https://docs.claude.com/en/docs/claude-code/cli-reference>
- Claude Code CLI usage: <https://code.claude.com/docs/en/cli-usage>
- Fresh demo install: `ops/fresh_demo_install.md`
- Slack app settings: `ops/slack_app_settings_guide.md`

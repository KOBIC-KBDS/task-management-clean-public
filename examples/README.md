# Task Management Examples

Run the credential-free Slack-DM dogfood demo:

```powershell
$env:TASK_CORE_PATH = "C:\path\to\llm-wiki"
python -X utf8 -m task_management.cli --state .task_management-demo-run demo `
  --fixture examples/slack_dogfood_demo.json `
  --output-dir out/demo `
  --task-core-root $env:TASK_CORE_PATH
```

The demo writes:

- `out/demo/dashboard.html`
- `out/demo/slack-monthly-task-page.md`
- `out/demo/task-core-preview.json`
- `out/demo/simulated-dm-outbox.md`
- `out/demo/summary.json`

Run a one-cycle private dogfood loop smoke test from a Slack transcript fixture:

```powershell
python -X utf8 -m task_management.cli --state .task_management-dogfood-smoke dogfood-loop `
  --transcript-input examples/slack_transcript_demo.json `
  --dashboard-output out/dogfood-dashboard.html `
  --interval-seconds 0 `
  --max-cycles 1 `
  --send
```

Run one real Claude Code semantic-backend smoke test after `claude` is logged in:

```powershell
$env:TASK_MANAGEMENT_OPERATING_AGENT = "claude"
$env:TASK_MANAGEMENT_CLAUDE_MODEL = "sonnet"
$env:TASK_MANAGEMENT_CLAUDE_FALLBACK = "0"
python -X utf8 -m task_management.cli --agent claude --state .task_management-claude-smoke `
  simulate --fixture examples/claude_smoke_private.json
python -X utf8 -m task_management.cli --state .task_management-claude-smoke events
```

Expected evidence: an `agent.decision.created` event with
`source="claude_code_cli"`.

Render the local web task page from any local state directory:

```powershell
python -X utf8 -m task_management.cli --state .task_management-demo-run render-dashboard --today 2026-05-05 --output out/dashboard.html
```

Export approved proposals as a task-core preview payload:

```powershell
python -X utf8 -m task_management.cli --state .task_management-demo-run export-preview --output out/preview.json
```

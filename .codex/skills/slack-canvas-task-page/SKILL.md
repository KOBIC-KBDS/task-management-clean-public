---
name: slack-canvas-task-page
description: Render and publish the task_management Slack personal monthly task page to a Slack Canvas using Slack Web API canvases.edit. Use when the user asks to update, replace, or refresh a Slack monthly/personal task Canvas from task_management state.
---

# Slack Canvas Task Page

Use this project-local skill when the task_management monthly task page should be rendered for Slack Canvas.

## Contract

- Render from local task_management SQLite/JSONL state.
- Preserve preview-only task-core behavior; do not write task-core inbox/raw/wiki files.
- Default to dry-run output unless the user explicitly requests a live Slack write.
- Live writes require a Slack token with `canvases:write` and the target `canvas_id`.
- Prefer replacing the whole personal monthly Canvas with a generated Markdown body when the user asks to “갈아엎기”.

## Dry run

```powershell
python -m task_management.cli --state .task-management-demo publish-slack-monthly-page `
  --month 2026-05 `
  --actor me `
  --canvas-id CANVAS_EXAMPLE_ID `
  --output out/slack-monthly-task-page-2026-05.md `
  --payload-output out/slack-monthly-task-page-2026-05.canvas-payload.json
```

## Live replace

Only after explicit user approval for the external Slack write:

```powershell
$env:SLACK_CANVAS_TOKEN='<token with canvases:write>'
python -m task_management.cli --state .task-management-demo publish-slack-monthly-page `
  --month 2026-05 `
  --actor me `
  --canvas-id CANVAS_EXAMPLE_ID `
  --output out/slack-monthly-task-page-2026-05.md `
  --payload-output out/slack-monthly-task-page-2026-05.canvas-payload.json `
  --send
```

## Verification

Run:

```powershell
python -X utf8 -m pytest -q
python -X utf8 -m compileall -q task_management tests
```

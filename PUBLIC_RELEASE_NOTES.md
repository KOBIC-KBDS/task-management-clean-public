# Public Release Notes

This repository is a clean public source snapshot of a Slack-first task-management adapter.

## What is included

- Source code for the task-management adapter.
- Sanitized example fixtures for local smoke tests.
- Setup guides for creating a separate Slack app in a new workspace.
- Tests that exercise semantic task intake, hierarchy/subtask rendering, Slack Socket Mode fixtures, and Claude Code CLI backend integration.

## What is intentionally not included

- Deployment-specific Slack tokens, app tokens, signing secrets, channel IDs, user IDs, canvas/document IDs, or workspace URLs.
- Runtime state such as SQLite databases, JSONL event logs, generated dashboards, or local transcript captures.
- Private operational repository history or pull-request history.
- Real Slack transcripts, real meeting notes, person names, locations, or organization-specific task content.

## Runtime boundary

Each deployment must create its own local state directory and configure its own Slack app credentials through local environment variables or an approved secret manager. Never commit runtime state or secrets back to this repository.

## Release hygiene

Before publishing future releases, scan both the current tree and git history for secrets, Slack identifiers, local paths, runtime files, and real transcripts. Public releases should be produced from a fresh-history export rather than by making a staging mirror public.

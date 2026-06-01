# Slack App Setup

This project is designed for private Slack app deployment.

## Runtime responsibility

The Slack app is an edge adapter, not the task-management brain. Slack only supplies:

- inbound message events,
- outbound DM replies,
- optional Canvas/dashboard publication.

The task-management authority remains local to this repository runtime: SQLite/JSONL state, deterministic proposal policy, task-core preview validation, and the semantic operating agent. The semantic agent can be `codex exec` using the operator's logged-in Codex session or Claude Code print mode using the operator's logged-in Claude session; these login-session modes do not require API keys in the runtime environment.

Because of that boundary, a new private deployment is just:

1. clone this repository,
2. log in to the chosen semantic CLI on the host that will operate it,
3. point `TASK_CORE_PATH` to an external task-core checkout,
4. create/install a private Slack app,
5. set that app's local tokens and DM/channel identifiers,
6. run the Slack loop against an isolated `--state` directory.

Do not share state DBs or Slack tokens between deployments unless they are intentionally operating the same queue.

For a screen-by-screen Slack app configuration checklist with screenshot
placeholders, see [`slack_app_settings_guide.md`](slack_app_settings_guide.md).

## Bot token scopes

- `chat:write`
- `im:history`
- `im:write`

## Socket Mode

Enable Socket Mode with an app-level token (`xapp-...`) and subscribe to `message.im` for direct-message intake. Use `slack-socket-doctor` before running the live loop.

## Secret handling

Keep all Slack tokens in local environment variables or an approved secret manager. Never commit `.env`, state directories, transcripts with private content, SQLite databases, JSONL logs, or generated dashboards containing private messages.

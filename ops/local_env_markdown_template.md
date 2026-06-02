# Local Clean Deployment Values

Copy this file to `.env.local.md` and paste values from your NEW Slack app.
Do not commit `.env.local.md`.

The CLI loads `.env.local` first and `.env.local.md` second. Markdown values
therefore override inherited shell variables and also override blank values from
`.env.local` when both files exist.

## Core runtime

| Key | Value | Notes |
| --- | --- | --- |
| TASK_CORE_PATH |  | Path to your external task-core checkout, if used. |
| TASK_MANAGEMENT_STATE | .task-management-demo | Use a new state directory per Slack app/workspace. |
| TASK_MANAGEMENT_SLACK_ACTOR_ID | me | Local actor label. |
| TASK_MANAGEMENT_INSTANCE_ID | clean-demo | Unique name for this machine/deployment. |
| TASK_MANAGEMENT_ALLOWED_INSTANCE_ID | clean-demo | Must match instance id to allow live sends. |
| TASK_MANAGEMENT_DASHBOARD_URL | http://127.0.0.1:8787/dashboard.html | Home tab dashboard link. REQUIRED for the App Home tab to refresh: slack-socket-loop / slack-fast-cycle publish the Home view only when this is set (empty value silently skips Home publish). |

## Semantic backend

| Key | Value | Notes |
| --- | --- | --- |
| TASK_MANAGEMENT_OPERATING_AGENT | claude | Use `claude` or `codex`. |
| TASK_MANAGEMENT_CLAUDE_MODEL | sonnet | Used when agent is `claude`. |
| TASK_MANAGEMENT_CLAUDE_FALLBACK | 0 | Fail closed if Claude Code is unavailable. |
| TASK_MANAGEMENT_CODEX_MODEL | gpt-5.5 | Used when agent is `codex`. |

## Slack app values

Paste these from the new Slack app/workspace only. Leave blank until the new app
is installed. Blank values clear inherited shell variables.

| Key | Value | Notes |
| --- | --- | --- |
| SLACK_BOT_TOKEN |  | Bot User OAuth Token from OAuth & Permissions. |
| SLACK_APP_TOKEN |  | App-level token with `connections:write` for Socket Mode. |
| SLACK_USER_ID |  | Your user id in the new workspace. |
| SLACK_DM_CHANNEL_ID |  | DM channel id, or leave blank and resolve with doctor. |
| SLACK_BOT_USER_ID |  | Optional bot user id. |
| TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS |  | Optional comma-separated channel ids. |
| TASK_MANAGEMENT_SLACK_WATCH_REQUIRE_MENTION | 1 | Default to mention-required channel watching. |

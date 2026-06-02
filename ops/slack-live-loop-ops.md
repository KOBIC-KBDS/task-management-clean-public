# Slack live loop operations

The Slack task-management runtime is a user-level `launchd` service. It is not
the Codex App Server and it is not tied to an interactive Codex session.

## Service identity

- Label: `com.example.task-management.slack-live`
- Repo: `/opt/task-management`
- State: `.task-management-live`
- Wrapper: `ops/slack-live-loop.sh`
- LaunchAgent: `~/Library/LaunchAgents/com.example.task-management.slack-live.plist`

## Start, stop, restart

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.task-management.slack-live.plist"
launchctl kickstart -k "gui/$(id -u)/com.example.task-management.slack-live"
```

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.task-management.slack-live.plist"
```

```sh
launchctl kickstart -k "gui/$(id -u)/com.example.task-management.slack-live"
```

## Status and logs

```sh
launchctl print "gui/$(id -u)/com.example.task-management.slack-live"
ps aux | grep -E 'slack-socket-loop|slack-live-loop' | grep -v grep
tail -f /opt/task-management/.task-management-live/logs/slack-live-loop.launchd.log
```

## Runtime model

The long-lived process is Python:

```sh
.venv/bin/python -X utf8 -m task_management.cli --state .task-management-live slack-socket-loop --send --actor me
```

Optional Slack notification triage is allowlist-based. Add channel IDs to
`.env.local` with:

```sh
TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS=CHANNEL_ID_A,CHANNEL_ID_B
TASK_MANAGEMENT_SLACK_WATCH_REQUIRE_MENTION=1
```

The socket loop will process only allowlisted Slack channel/group messages that
mention `SLACK_USER_ID` by default. Notification-derived proposals are stored as
confirmation-required candidates and are not approved until the user accepts the
approval request in the personal DM.

When a Slack message needs semantic interpretation, that process may spawn a
short-lived `codex exec --cd /opt/task-management ...` child.
Those child processes are per-message workers, not the durable service identity.

If the Python process is killed while the LaunchAgent is loaded, `launchd`
restarts it. To intentionally stop it, unload the LaunchAgent with `bootout`
instead of killing only the PID.

## Troubleshooting

- Socket Mode connect fails with `WinError 10054` / connection reset: the
  `apps.connections.open` WebSocket handshake is intermittently blocked by a
  firewall/proxy. Restart the loop; it usually reconnects. If HTTP paths
  (`slack-doctor --live-open-dm`, `slack-fast-cycle`) stay healthy, it is a
  network/WSS issue, not a token or config problem.
- Slow semantic replies: prefer `TASK_MANAGEMENT_CLAUDE_MODEL=sonnet`; `opus`
  is noticeably slower for this interactive DM loop.
- The socket loop shows up as two `python` processes (a launcher and the real
  interpreter, parent/child). That is one instance, not a duplicate — check the
  parent/child PIDs before terminating.
- Read receipts: receiving a message adds an `eyes` reaction and completion adds
  `white_check_mark`; this needs the `reactions:write` bot scope and is
  best-effort (missing scope only skips the reactions, not the reply).

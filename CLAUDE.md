# Claude Code Operator Notes

This repository can be edited from Claude Code and can also use Claude Code as
the semantic operating-agent backend with `TASK_MANAGEMENT_OPERATING_AGENT=claude`.
Read these files first:

1. `AGENTS.md` — project rules, UTF-8/Korean safety, task-core boundary, and verification.
2. `ops/claude_code_backend_handoff.md` — Claude Code backend setup and smoke
   validation.
3. `ops/fresh_demo_install.md` and `ops/slack_app_settings_guide.md` — clean
   Slack demo setup.

Important constraints:

- Never commit Slack tokens, `.env`, `.env.local`, SQLite DBs, JSONL logs,
  generated dashboards, or real Slack transcripts.
- Keep task-core external through `TASK_CORE_PATH`; do not copy task-core into
  this repo.
- Preserve UTF-8 for Korean text. For live/state side effects involving Korean,
  use UTF-8 files or scripts rather than shell inline text.
- The semantic agent may only return strict operating-agent decisions. The
  deterministic Python core owns persistence, approvals, Slack sends, and
  task-core preview validation.

Verification:

```bash
python -X utf8 -m pytest -q
```

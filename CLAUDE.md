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

## Model selection (ask before defaulting)

When you install or configure this repository for a user (e.g. "clone/install this
repo for me"), do not silently pick the Claude model — ask the user first and flag
token cost:

- `opus` reasons the deepest but its token cost is high for an always-on Socket
  Mode DM loop; confirm the user accepts that cost before choosing it.
- `sonnet` (alias resolves to the latest) is the recommended cost-conscious default
  for everyday dogfooding. Because Claude token usage is significant, raise `sonnet`
  as an option even if the user initially asks for opus.
- `TASK_MANAGEMENT_CLAUDE_EFFORT` (low|medium|high|xhigh|max) tunes latency/depth
  without switching models.

Set `TASK_MANAGEMENT_CLAUDE_MODEL` only after the user confirms the choice. The
Codex backend likewise takes `TASK_MANAGEMENT_CODEX_MODEL` / `TASK_MANAGEMENT_CODEX_EFFORT`.

Verification:

```bash
python -X utf8 -m pytest -q
```

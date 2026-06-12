# Shared Engineering Context

This file is the shared working memory for Codex, Claude Code, and human maintainers. Update it when you make a meaningful code, test, documentation, or operating-policy change so the next worker can continue without re-reading the entire repository or private chat history.

Keep this file public-safe:

- Do not include Slack tokens, channel IDs, user IDs, local state paths, SQLite contents, JSONL event excerpts, or raw private transcripts.
- Use sanitized examples such as `search page direction request`, `13:30 follow-up meeting`, or `example data portal`.
- Link to committed code/docs/tests instead of copying private runtime evidence.
- Record decisions and verification evidence, not hidden reasoning.

## How to update this file

1. Add a row to **Change Log** for completed work.
2. Add or adjust **Current Refactor Direction** when the intended architecture changes.
3. Add unresolved questions to **Open Questions / Watchpoints**.
4. If a change affects runtime flow, also update `docs/agent-flow.md` and the rendered `docs/agent-flow.html`.

## Current Refactor Direction

- Keep the semantic operating agent as the intent engine: it should classify messages, choose semantic targets, and return strict JSON.
- Keep deterministic code as the safety core: it validates targets, rejects low-confidence or malformed patches, applies approval/missing-slot/conflict policy, persists audit state, and renders user-facing surfaces.
- Prefer semantic evidence over brittle exact-message matching. Regression tests should use sanitized scenarios that represent a class of failures, not a single private incident.
- Preserve clean-install boundaries: blank token templates, no runtime DB/log files, no private Slack identifiers, and no task-core source copied into this repo.
- Preserve preview-only task-core behavior unless a future task explicitly changes the write contract.

## Current Runtime Safety Rules

- A pending approval/request may be updated only when the incoming message provides credible target evidence or an explicit request/proposal identifier.
- A single pending clarification is not enough by itself to hijack an unrelated new task/event/meeting.
- User rejection signals must close the pending approval/proposal as `rejected` and remove remaining missing-slot pressure from human surfaces.
- If a semantic patch looks like unrelated new work, deterministic code should reject that target and route the same message through new-work intake.
- Plain `question` items do not require a date unless they explicitly carry `needs_exact_date=true` or another concrete date requirement.
- Rejected and done proposals are excluded from task-core preview readiness, not marked as blocked work.

## Change Log

| Date | Actor | Area | What changed | Verification |
| --- | --- | --- | --- | --- |
| 2026-06-13 | Codex | semantic routing / approvals / surfaces | Ported pending-card hijack protection: semantic rejection patches now close approvals, low-evidence meeting-like patches fall back to new-work creation, half-hour meeting parsing defaults to today for event context, public-safe source questions no longer require dates, and rejected/done items are excluded from blocked preview counts. | `python -X utf8 -m pytest -q` on clean tree via shared test venv: 258 passed. |

## Open Questions / Watchpoints

- Refactors should not move semantic target validation solely into prompts; deterministic target-evidence checks are the safety net.
- If new agent backends are added, they must return the same strict decision envelope and must be covered by fixture tests.
- If Slack Home, briefing, or dashboard rendering changes, confirm all three surfaces keep the same hierarchy/preview semantics.
- If task/event duplicate collapse is broadened, keep it conservative and preserve an audit trail for merged duplicates.

## Useful Entry Points

- Runtime map: `docs/agent-flow.md`
- Clean release boundary: `docs/clean-release-boundary.md`
- Slack setup guides: `ops/slack_bot_connection_guide.md`, `ops/slack_app_settings_guide.md`, `ops/fresh_demo_install.md`
- Backend handoff: `ops/claude_code_backend_handoff.md`
- Core orchestration: `task_management/orchestrator.py`
- Semantic prompt contract: `task_management/operating_agent_prompt.py`
- Slot policy: `task_management/slot_validator.py`
- Human surfaces: `task_management/frontend.py`, `task_management/slack_home.py`, `task_management/secretary.py`

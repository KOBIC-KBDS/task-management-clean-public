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
- Keep the orchestrator as a thin coordinator. New runtime behavior should land in focused services such as proposal intake, semantic patch application, approval flow, workflow batch handling, outbound delivery, source/channel refs, time parsing, or surface registries before it grows the coordinator again.

## Current Runtime Safety Rules

- A pending approval/request may be updated only when the incoming message provides credible target evidence or an explicit request/proposal identifier.
- A single pending clarification is not enough by itself to hijack an unrelated new task/event/meeting.
- User rejection signals must close the pending approval/proposal as `rejected` and remove remaining missing-slot pressure from human surfaces.
- Rejected proposals are audit/history records, not active work. Keep them out of schedules, hierarchy child rollups, pending confirmation cards, Slack Home, briefings, monthly pages, dashboards, and Slack digests while preserving status/preview counts where those counts are explicitly historical.
- Completion is terminal even when a proposal still has missing date/time slots. Request-scoped completion should close the request and then mark the proposal done, rather than continuing to ask for the missing slot.
- If a semantic patch looks like unrelated new work, deterministic code should reject that target and route the same message through new-work intake.
- Plain `question` items do not require a date unless they explicitly carry `needs_exact_date=true` or another concrete date requirement.
- Rejected and done proposals are excluded from task-core preview readiness, not marked as blocked work.
- General private-DM instructions may detach explicit direct leaf children from an existing workflow while completing or retaining the old parent. Reparenting and nested-workflow moves remain clarification-only.
- Multi-proposal workflow changes and their audit events must be persisted together through the transactional audit outbox. JSONL delivery is a retryable projection, not a reason to report a committed hierarchy change as failed.

## Change Log

| Date | Actor | Area | What changed | Verification |
| --- | --- | --- | --- | --- |
| 2026-06-13 | Codex | OMC refactor sync / architecture | Ported the large OMC runtime refactor into the clean public branch: Slack delivery retries and stable ids, same-day deferred reminders, extracted approval/proposal/semantic/workflow services, provider-generic outbound delivery, channel/source reference registries, unified Korean time sorting, kind/reconciler/surface registries, prep-subtask builder, runtime guards, and updated CodeBoarding inventory. Clean-only install defaults and public-safe paths were preserved. | `clean_mirror_sync.py check-worktree --base main`: ok; `python -X utf8 -m pytest -q`: 296 passed; `git diff --check`: ok; public-safety grep for tokens/Slack ids/private IPs: no hits. |
| 2026-06-13 | Codex | semantic routing / approvals / surfaces | Ported pending-card hijack protection: semantic rejection patches now close approvals, low-evidence meeting-like patches fall back to new-work creation, half-hour meeting parsing defaults to today for event context, public-safe source questions no longer require dates, and rejected/done items are excluded from blocked preview counts. | `python -X utf8 -m pytest -q` on clean tree via shared test venv: 258 passed. |
| 2026-06-13 | Codex | completion / rejected surfaces | Ported the rejected-work surface boundary and unresolved-slot completion behavior: rejected proposals stay auditable but no longer render as active schedule/current-work/pending-card rows, and completion updates can close request-scoped or direct targets even when date/time slots remain unknown. | `clean_mirror_sync.py check-worktree --base main`: ok; targeted semantic/frontend/monthly tests: 26 passed; `python -X utf8 -m pytest -q`: 301 passed; `git diff --check`: ok. |
| 2026-07-30 | Codex | general Slack input / workflow restructuring | Added bounded semantic context for broad existing-work instructions and a validated `workflow_restructure` operation that can detach explicit direct leaf children while completing or retaining the old parent. Unsafe reparenting, nested moves, unauthorized targets, and pending approval states ask for clarification or reject without mutation. Related proposal and audit rows use a recoverable transactional outbox. | Live commit `f6548d2`; clean verification recorded in the sync commit. |

## Open Questions / Watchpoints

- Refactors should not move semantic target validation solely into prompts; deterministic target-evidence checks are the safety net.
- If new agent backends are added, they must return the same strict decision envelope and must be covered by fixture tests.
- If Slack Home, briefing, or dashboard rendering changes, confirm all three surfaces keep the same hierarchy/preview semantics.
- If task/event duplicate collapse is broadened, keep it conservative and preserve an audit trail for merged duplicates.
- If reparenting or nested workflow moves become supported, define approval-request migration, dependency preservation, cycle prevention, and rollback semantics before expanding the deterministic operation vocabulary.
- If service extraction continues, keep the public docs in sync: `docs/agent-flow.md` is the operator narrative, while `docs/codeboarding/` is static inventory evidence and should not contain local absolute paths.

## Useful Entry Points

- Runtime map: `docs/agent-flow.md`
- Clean release boundary: `docs/clean-release-boundary.md`
- Slack setup guides: `ops/slack_bot_connection_guide.md`, `ops/slack_app_settings_guide.md`, `ops/fresh_demo_install.md`
- Backend handoff: `ops/claude_code_backend_handoff.md`
- Core orchestration: `task_management/orchestrator.py`
- Service seams: `task_management/proposal_intake.py`, `task_management/semantic_patch_service.py`, `task_management/approval_flow.py`, `task_management/workflow_batch.py`, `task_management/outbound_delivery.py`
- Semantic prompt contract: `task_management/operating_agent_prompt.py`
- Slot policy: `task_management/slot_validator.py`
- Human surfaces: `task_management/frontend.py`, `task_management/slack_home.py`, `task_management/secretary.py`

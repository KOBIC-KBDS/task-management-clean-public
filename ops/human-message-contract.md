# Human message contract

This runtime is a personal Slack DM assistant for one user. Human-facing Slack
messages should ask only for replies that are currently useful to that user.

## Attention contract

The assistant may say there is nothing to confirm only when there are no:

- due or overdue progress checks for approved/applied task-like proposals;
- reached deferred reminders in `metadata["deferred_until"]` where missing human
  input remains;
- pending missing-slot questions;
- pending approval decisions.

## Relation metadata

Relations are renderer metadata only. They do not change the persistence schema.

- `parent_proposal_id`: child proposal rendered under this parent when both are
  visible in the same section.
- `depends_on_proposal_id` or `depends_on_proposal_ids`: comma-separated blocker
  proposal IDs checked before downstream completion nags.
- `step_index` and `step_count`: optional display labels for workflow children.
- `workflow_id` and `workflow_title`: optional grouping hints for future UI.

Dependencies are considered complete only when the dependency proposal is
`done` or `rejected`.

Future deferred missing input is quiet until `deferred_until` is reached. While
it is quiet, morning briefings and proactive due-work checks should not ask for
completion/progress on that proposal.

Dependency lookup may inspect non-personal proposals so a personal downstream
item can explain its blocker first, but rendering remains scoped to the
personal Slack DM.

## Personal DM wording

End-of-day reviews should be scoped to the user's remaining items and must not
sound like a team-wide status rollup. Completed items may be listed as evidence
but must not be asked again.

## Explicit request marker

Private-DM task/planning/app conversation may receive a normal read-only answer
without a command prefix or task mutation. `[요청]` is an optional message-level
instruction marker. When present, the body must be answered, converted into a
supported validated mutation, or met with a concrete clarification; it must not
silently become `no_action`. The marker does not bypass target, approval, risk,
or mutation gates. It must not be copied into a task title, item type, or
persisted task metadata merely because the user wrote it.

- Read-only asks such as explanation, status, reason, or “what should I do?” use
  an `agent_direct_response` message and leave proposal/approval state unchanged.
- The rendered answer begins with `*[요청]*` only when the literal tag was present;
  untagged messages should look like ordinary conversation.
- A message may ask for both an explanation and a real task change; render the
  answer and apply only the independently validated draft/patch.
- Unsupported semantic update keys reject the whole patch; never apply only the
  easy title/date fields while silently dropping the requested operation.
- If the target is ambiguous, ask which existing item the user means. Do not
  create a new task from the question and do not disguise the answer as a
  rejected clarification patch.

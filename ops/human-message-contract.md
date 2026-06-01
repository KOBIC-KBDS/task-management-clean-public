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

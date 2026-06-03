# Clean Release Boundary

This repository is the public clean release of the task-management adapter. It
should be strengthened from clean staging and operational development only after
the change is scrubbed, testable, and useful to a fresh installer.

## Import direction

When reconciling clean staging with this public repository, preserve the public
repository as the release target:

1. Keep public setup, install, and demo guidance when it is more complete.
2. Import missing clean-staging guidance only after removing private source
   commit IDs, local operator paths, Slack workspace identifiers, real names,
   transcripts, runtime state, and deployment-specific assumptions.
3. Prefer a fresh public commit over merging private or staging history directly.

## Do not publish

Never commit or publish:

- Slack bot/app tokens, signing secrets, workspace URLs, channel IDs, user IDs,
  canvas/document IDs, or token-bearing environment files.
- Local SQLite databases, JSONL event logs, generated dashboards, transcript
  captures, launchd logs, or machine-specific state directories.
- Private operational repository history, private pull-request history, or source
  commit IDs from the operational repository.
- Real meeting notes, real person names, organization-specific labels, or
  non-anonymized examples.
- The external `task-core` source tree.

## What may be imported

Public-safe imports include:

- Deterministic orchestration, rendering, test, and preview-bridge code.
- Sanitized fixtures that contain no real transcripts or workspace identifiers.
- Setup guides for creating a separate Slack app in a new workspace.
- Clean demo instructions that start with blank tokens and local state generated
  on first run.
- Documentation explaining where Codex or Claude Code acts as the semantic
  operating agent.

## Verification before publish

Before pushing a strengthened public clean release:

1. Compare the clean staging and public trees by file, not only by commit
   ancestry. These histories may intentionally be unrelated.
2. Inspect any staging-only file before importing it.
3. Run the test suite with UTF-8 enabled.
4. Run `git diff --check`.
5. Scan tracked files for state/secrets/runtime artifacts.
6. Confirm the final local `main` matches `origin/main` after push.


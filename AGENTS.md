# Example Lab Task Management

This repository is a private Slack-first team task-management adapter. It references task-core externally and must not copy task-core into this repository.

## Product North Star

The alpha and omega of this project is **LLM-powered semantic orchestration**:
the system must read Korean work messages in context, understand intent, and convert them into normalized task/event/routine proposals or targeted updates.

- The operating agent is the semantic engine: it decides what the message means, which existing proposal it refers to, what slots are filled, and what remains ambiguous.
- Slack is only a channel adapter. The source of truth is this repo runtime plus its local state and semantic agent; each private deployment may use its own Slack app/bot and local Codex login session.
- Deterministic code is the safety core: it validates the strict decision envelope, applies approval/missing-slot/conflict policy, persists audit state, renders human messages, and builds preview-only task-core payloads.
- Rule/keyword parsing is allowed only as a fallback, fixture aid, or guardrail. It must not become the primary product behavior for live team dogfood.
- Any implementation that cannot explain semantic target, confidence, evidence, and unresolved assumptions is not aligned with the project goal.

## Local Rules

- Keep task-core as an external dependency. Prefer `TASK_CORE_PATH` or an explicitly configured private package; never copy task-core code here.
- The adapter may build `task-core.export.v1` preview payloads and validate them, but it must not write task-core inbox/raw/wiki files unless a future task explicitly changes that contract.
- Prefer UTF-8 for Korean team discussion text.
- Korean/UTF-8 is a hard safety rule, not a style preference:
  - Do not put raw Korean literals inside `shell_command` command strings,
    PowerShell here-strings, `echo`, pipes, or native-process stdin.
  - If a command must create/modify/send Korean text, write a UTF-8 file first
    from Python (`python -X utf8`) using Unicode escapes or an already UTF-8
    source file, then pass that file path to the CLI (`--text-file`,
    transcript file, JSON fixture, etc.).
  - For live Slack corrections and DB/state repairs, only use UTF-8 file based
    inputs. Never paste Korean correction bodies through PowerShell inline
    commands; that path has already produced mojibake in Slack and local state.
  - After changing Korean text, run the UTF-8/mojibake guard tests before
    claiming completion.
- Start with Slack private/team app flows. Do not add other live chat/calendar integrations without explicit request.
- Never commit Slack tokens, app tokens, local state DBs, JSONL event logs, or unanonymized transcripts.

## Verification

Run:

```powershell
python -X utf8 -m pytest -q
```

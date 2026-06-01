from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping, Protocol, Sequence

from .domain import ApprovalRequest, IncomingMessage, Proposal
from .operating_agent import (
    OPERATING_AGENT_SCHEMA,
    OPERATING_DECISION_OUTPUT_SCHEMA,
    TeamTaskOperatingAgent,
    OperatingAgentDecision,
    RuleBasedTeamTaskOperatingAgent,
    decision_from_payload,
)
from .semantic_context import build_operating_agent_context


class ClaudeCodeOperatingAgentError(RuntimeError):
    """Raised when the optional Claude Code CLI adapter cannot produce a valid decision."""


@dataclass(frozen=True)
class ClaudeCodeCliOperatingAgentConfig:
    claude_bin: str = "claude"
    model: str = ""
    cwd: Path = Path.cwd()
    timeout_seconds: float = 180.0
    fallback_on_error: bool = False
    permission_mode: str = "plan"
    max_turns: int = 3
    no_session_persistence: bool = True
    tools: str = ""
    strip_api_key_env: bool = True

    @classmethod
    def from_env(cls) -> "ClaudeCodeCliOperatingAgentConfig":
        return cls(
            claude_bin=os.environ.get("TASK_MANAGEMENT_CLAUDE_BIN", "claude"),
            model=os.environ.get("TASK_MANAGEMENT_CLAUDE_MODEL", ""),
            cwd=Path(os.environ.get("TASK_MANAGEMENT_CLAUDE_CWD", str(Path.cwd()))),
            timeout_seconds=float(os.environ.get("TASK_MANAGEMENT_CLAUDE_TIMEOUT_SECONDS", "180")),
            fallback_on_error=os.environ.get("TASK_MANAGEMENT_CLAUDE_FALLBACK", "0").lower()
            not in {"0", "false", "no", "off"},
            permission_mode=os.environ.get("TASK_MANAGEMENT_CLAUDE_PERMISSION_MODE", "plan"),
            max_turns=int(os.environ.get("TASK_MANAGEMENT_CLAUDE_MAX_TURNS", "3")),
            no_session_persistence=os.environ.get("TASK_MANAGEMENT_CLAUDE_NO_SESSION_PERSISTENCE", "1").lower()
            not in {"0", "false", "no", "off"},
            tools=os.environ.get("TASK_MANAGEMENT_CLAUDE_TOOLS", ""),
            strip_api_key_env=os.environ.get("TASK_MANAGEMENT_CLAUDE_STRIP_API_KEY_ENV", "1").lower()
            not in {"0", "false", "no", "off"},
        )


class ClaudeCodeRunner(Protocol):
    def run_decision(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        config: ClaudeCodeCliOperatingAgentConfig,
    ) -> Mapping[str, Any]:
        """Return the decoded Claude Code print-mode JSON envelope."""


class SubprocessClaudeCodeRunner:
    """Run Claude Code print mode against the already authenticated local CLI session."""

    def run_decision(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        config: ClaudeCodeCliOperatingAgentConfig,
    ) -> Mapping[str, Any]:
        command = _claude_command(config, schema=schema)
        completed = subprocess.run(  # noqa: S603 - executable is explicit user/local config
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=config.cwd,
            env=_subprocess_env(config),
            timeout=config.timeout_seconds,
            check=False,
        )
        stdout = completed.stdout.strip()
        envelope = _loads_json_object(stdout, "Claude Code stdout") if stdout else {}
        if completed.returncode != 0:
            detail = _claude_error_detail(envelope) or completed.stderr.strip() or stdout
            raise ClaudeCodeOperatingAgentError(f"claude print mode failed with exit={completed.returncode}: {detail}")
        if envelope.get("is_error") is True:
            raise ClaudeCodeOperatingAgentError(_claude_error_detail(envelope) or "claude print mode returned is_error=true")
        if not envelope:
            raise ClaudeCodeOperatingAgentError("claude print mode produced no JSON envelope")
        return envelope


class ClaudeCodeCliOperatingAgent:
    """Claude Code CLI operating agent.

    The adapter delegates semantic interpretation to the operator's local
    Claude Code login session in print mode. It does not mutate storage, approve
    proposals, send Slack messages, or write task-core files; the deterministic
    core remains the state owner.
    """

    def __init__(
        self,
        config: ClaudeCodeCliOperatingAgentConfig | None = None,
        *,
        runner: ClaudeCodeRunner | None = None,
        fallback_agent: TeamTaskOperatingAgent | None = None,
    ) -> None:
        self.config = config or ClaudeCodeCliOperatingAgentConfig.from_env()
        self.runner = runner or SubprocessClaudeCodeRunner()
        self.fallback_agent = fallback_agent or RuleBasedTeamTaskOperatingAgent()

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        fallback_decision = self.fallback_agent.decide(
            message,
            pending_approval_requests=pending_approval_requests,
            pending_proposals=pending_proposals,
        )
        try:
            prompt = self._prompt(
                message,
                pending_approval_requests=pending_approval_requests,
                pending_proposals=pending_proposals,
                fallback_decision=fallback_decision,
            )
            envelope = self.runner.run_decision(
                prompt,
                schema=OPERATING_DECISION_OUTPUT_SCHEMA,
                config=self.config,
            )
            decision = decision_from_payload(_decision_payload_from_envelope(envelope))
            return replace(
                decision,
                source="claude_code_cli",
                rationale=_with_claude_note(decision.rationale, self.config.model),
            )
        except Exception as exc:  # noqa: BLE001 - boundary catches model/process/schema failures for home-server stability
            if not self.config.fallback_on_error:
                raise ClaudeCodeOperatingAgentError(str(exc)) from exc
            return replace(
                fallback_decision,
                source="claude_code_cli_fallback",
                rationale=f"Claude Code CLI operating agent unavailable or invalid; used rule fallback. reason={exc}",
            )

    def _prompt(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
        fallback_decision: OperatingAgentDecision,
    ) -> str:
        context = build_operating_agent_context(
            message,
            pending_approval_requests=pending_approval_requests,
            pending_proposals=pending_proposals,
            fallback_decision=fallback_decision,
        )
        context["rules"]["auth_policy"] = "Use the local Claude Code login session only; do not ask for or output API keys."
        return (
            _SYSTEM_INSTRUCTIONS
            + "\n\nReturn exactly one JSON object matching the provided output schema.\n"
            + "Every proposal_patch must include target_confidence, evidence_text, assumptions, missing_slots, and needs_clarification.\n"
            + "<task_management_context>\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n</task_management_context>\n"
        )


def _claude_command(config: ClaudeCodeCliOperatingAgentConfig, *, schema: Mapping[str, Any]) -> list[str]:
    command = [_resolve_claude_bin(config.claude_bin), "-p"]
    if config.model:
        command.extend(["--model", config.model])
    if config.no_session_persistence:
        command.append("--no-session-persistence")
    if config.permission_mode:
        command.extend(["--permission-mode", config.permission_mode])
    command.extend(["--tools", config.tools])
    if config.max_turns > 0:
        command.extend(["--max-turns", str(config.max_turns)])
    command.extend(
        [
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False, sort_keys=True),
        ]
    )
    return command


def _decision_payload_from_envelope(envelope: Mapping[str, Any]) -> Mapping[str, object]:
    structured = envelope.get("structured_output")
    if isinstance(structured, Mapping):
        return structured
    result = envelope.get("result")
    if isinstance(result, str) and result.strip():
        return _loads_json_object(result, "Claude Code result")
    raise ClaudeCodeOperatingAgentError("Claude Code envelope did not include structured_output or JSON result")


def _loads_json_object(text: str, label: str) -> Mapping[str, object]:
    stripped = text.strip()
    if not stripped:
        raise ClaudeCodeOperatingAgentError(f"{label} was empty")
    for candidate in _json_candidates(stripped):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            return payload
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            return payload
    raise ClaudeCodeOperatingAgentError(f"{label} did not contain a JSON object")


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    if text.startswith("```"):
        fenced = text.strip("`").strip()
        if fenced.lower().startswith("json"):
            fenced = fenced[4:].strip()
        candidates.append(fenced)
    if "`" in text:
        parts = text.split("`")
        candidates.extend(part.strip() for part in parts if part.strip().startswith("{"))
    return candidates


def _claude_error_detail(envelope: Mapping[str, Any]) -> str:
    result = envelope.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    api_error = envelope.get("api_error_status")
    if api_error:
        return str(api_error)
    return ""


def _resolve_claude_bin(claude_bin: str) -> str:
    resolved = shutil.which(claude_bin)
    if resolved:
        return resolved
    if os.name == "nt" and not claude_bin.lower().endswith((".cmd", ".exe", ".ps1")):
        for suffix in (".cmd", ".exe"):
            resolved = shutil.which(f"{claude_bin}{suffix}")
            if resolved:
                return resolved
    return claude_bin


_CLAUDE_API_KEY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_API_KEY",
)


def _subprocess_env(config: ClaudeCodeCliOperatingAgentConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.strip_api_key_env:
        for key in _CLAUDE_API_KEY_ENV_VARS:
            env.pop(key, None)
    return env


def _with_claude_note(rationale: str, model: str) -> str:
    note = "claude_code_login_session"
    if model:
        note = f"{note}; model={model}"
    return f"{rationale} ({note})" if rationale else note


_SYSTEM_INSTRUCTIONS = """You are the operating agent for a task_management task-management system.
You run through Claude Code CLI using the user's local Claude Code login session.
Return exactly one JSON object matching task-task_management.operating-agent.v1.
You do not mutate storage, approve proposals, write task-core files, or send calendar/Slack messages.
Your job is only to interpret the current message and emit proposal drafts, proposal patches, or no_action.
The deterministic core will enforce missing slots, approvals, idempotency, audit logs, and preview-only task-core export.
Preserve Korean text as UTF-8. Use concise Korean titles when appropriate.
Use source_key values that are stable for the message, such as claude/<message_id>/1.
Use assigned_to only from me, teammate, shared, unassigned.
Use item_type only from task, event, routine, reference, question, decision.
If a user implies a category outside those item_type values, do not invent a new item_type. Use the closest existing type only when its operational behavior fits; otherwise ask a clarification question or create a decision item with metadata type_policy_needed=true and type_request=<requested label>.
For allowlisted Slack notification messages (message.visibility=team and message_id starts with slack/), be conservative: create proposals only for clear actionable requests or commitments directed at the configured user; otherwise return no_action. Do not emit feedback patches from notification messages. The deterministic core will ask the user for confirmation before approving any notification-derived proposal.
For ambiguous date windows, put date_window_start/date_window_end/needs_exact_date in metadata and leave scheduled_date empty.
If the message says a tentative future date will be decided in an already scheduled discussion, do not ask for the future exact date/time now. Emit the tentative future item as item_type=decision with disposition=decision_pending, keep the date_window metadata, omit needs_exact_date/needs_exact_time, and mark date_resolution_policy=decide_in_scheduled_discussion.
For work/research discussions with a named external participant and no explicit venue requirement, set location_optional=true. Do not ask for a place merely because the discussion has a scheduled date/time.
When named non-task_management 담당자 appear, keep assigned_to as the internal owner and store the named counterpart in metadata.external_owner/external_participants/participant_label with participants=me/teammate/shared as applicable.
Use relation metadata when the message creates or updates dependent workflow items: parent_proposal_id, parent_source_key, depends_on_proposal_ids, depends_on_source_keys, step_index, step_count, workflow_id, workflow_title, workflow_role, risk_level, risk_reason, requires_separate_approval.
When the message is a high-confidence sequence, use semantic-split-default: emit one parent proposal with workflow_role=parent and ordered children. Mark risky or ambiguous children with requires_separate_approval=true instead of including them in grouped approval.
For follow-up batches tied to a pending or approved review task, preserve parent_proposal_id and inherit the review date/time unless the message overrides it.
For feedback, resolve the semantic target proposal/request from pending_proposal_cards before filling slots. Treat a message as feedback only when it contains explicit target evidence: a request/proposal id, direct title/semantic handle match, or reply/anaphora wording such as 이 건/방금 말한/그 일정/해당 일정. If the current message introduces a new task/event/routine without that evidence, emit create_proposals even when pending_proposal_cards exist.
If one human reply updates multiple pending proposals, emit multiple proposal_patches.
For completion/progress/deferral/confirmation/correction feedback, emit proposal_patches rather than new tasks. Put status=done and semantic_update_type=completion for full completion; progress_status=partial, progress_percent, remaining_work, and semantic_update_type=progress for partial progress; due_date/scheduled_date/time_window plus semantic_update_type=deferral for deferrals; status=confirmed plus semantic_update_type=confirmation for temporal/slot confirmations that should not complete the proposal; and corrected title or metadata slots such as corrected_title/title, external_owner, external_participants, participant_label, participants, location/location_optional plus semantic_update_type=correction for explicit corrections. Corrections must not set status. If only preparation/materials/subtask work is complete while the parent task/event remains open, include status=done, progress_status=complete, completion_scope=preparation|materials|subtask, semantic_update_type=completion so core records progress rather than closing the parent.
If target confidence is below 0.65 or the target is ambiguous, set needs_clarification=true and ask for clarification rather than guessing.
Every proposal_patch must include target_confidence, evidence_text, assumptions, missing_slots, and needs_clarification.
If the deterministic_baseline is sufficient, you may return an equivalent decision with improved title/metadata only.
Never request, print, or depend on an API key.
"""

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
from .operating_agent_prompt import build_operating_agent_system_instructions
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


_SYSTEM_INSTRUCTIONS = build_operating_agent_system_instructions(
    backend_runtime="Claude Code CLI using the user's local Claude Code login session",
    source_key_prefix="claude",
)

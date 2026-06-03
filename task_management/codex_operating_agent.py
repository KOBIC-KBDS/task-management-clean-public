from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Protocol, Sequence

from .domain import ApprovalRequest, IncomingMessage, Proposal
from .operating_agent import (
    OPERATING_AGENT_SCHEMA,
    TeamTaskOperatingAgent,
    OperatingAgentDecision,
    RuleBasedTeamTaskOperatingAgent,
    decision_from_payload,
)
from .operating_agent_prompt import build_operating_agent_system_instructions
from .semantic_context import build_operating_agent_context


class CodexOperatingAgentError(RuntimeError):
    """Raised when the optional Codex CLI operating-agent adapter cannot produce a valid decision."""


@dataclass(frozen=True)
class CodexCliOperatingAgentConfig:
    codex_bin: str = "codex"
    model: str = ""
    cwd: Path = Path.cwd()
    timeout_seconds: float = 180.0
    fallback_on_error: bool = True
    sandbox: str = "read-only"
    ask_for_approval: str = "never"
    reasoning_effort: str = ""
    strip_api_key_env: bool = True

    @classmethod
    def from_env(cls) -> "CodexCliOperatingAgentConfig":
        return cls(
            codex_bin=os.environ.get("TASK_MANAGEMENT_CODEX_BIN", "codex"),
            model=os.environ.get("TASK_MANAGEMENT_CODEX_MODEL", ""),
            cwd=Path(os.environ.get("TASK_MANAGEMENT_CODEX_CWD", str(Path.cwd()))),
            timeout_seconds=float(os.environ.get("TASK_MANAGEMENT_CODEX_TIMEOUT_SECONDS", "180")),
            fallback_on_error=os.environ.get("TASK_MANAGEMENT_CODEX_FALLBACK", "1").lower() not in {"0", "false", "no", "off"},
            sandbox=os.environ.get("TASK_MANAGEMENT_CODEX_SANDBOX", "read-only"),
            ask_for_approval=os.environ.get("TASK_MANAGEMENT_CODEX_APPROVAL", "never"),
            reasoning_effort=os.environ.get("TASK_MANAGEMENT_CODEX_EFFORT", ""),
            strip_api_key_env=os.environ.get("TASK_MANAGEMENT_CODEX_STRIP_API_KEY_ENV", "1").lower()
            not in {"0", "false", "no", "off"},
        )


class CodexExecRunner(Protocol):
    def run_decision(self, prompt: str, *, schema: Mapping[str, Any], config: CodexCliOperatingAgentConfig) -> str:
        """Return the final Codex response text."""


class SubprocessCodexExecRunner:
    """Run `codex exec` against the already logged-in local Codex session."""

    def run_decision(self, prompt: str, *, schema: Mapping[str, Any], config: CodexCliOperatingAgentConfig) -> str:
        with tempfile.TemporaryDirectory(prefix="task_management-codex-agent-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "operating-agent.schema.json"
            output_path = tmp_path / "decision.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
            codex_bin = _resolve_codex_bin(config.codex_bin)
            command = [
                codex_bin,
                "exec",
                "--ephemeral",
                "--cd",
                str(config.cwd),
                "--sandbox",
                config.sandbox,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if config.model:
                command[2:2] = ["--model", config.model]
            # Optional reasoning-effort override for `codex exec`. Empty by default so the
            # user's ~/.codex/config.toml (model_reasoning_effort) governs. When set
            # (TASK_MANAGEMENT_CODEX_EFFORT=minimal|low|medium|high|xhigh) it scales reasoning
            # DEPTH (reasoning tokens, e.g. low~15 vs xhigh~516 on a hard prompt), NOT
            # wall-clock latency: `codex exec` session/tool startup overhead dominates time.
            if config.reasoning_effort:
                command[2:2] = ["-c", f'model_reasoning_effort="{config.reasoning_effort}"']
            env = os.environ.copy()
            if config.strip_api_key_env:
                env.pop("OPENAI_API_KEY", None)
                env.pop("CODEX_API_KEY", None)
            completed = subprocess.run(  # noqa: S603 - executable is explicit user/local config
                command,
                input=prompt,
                text=True,
                encoding="utf-8",  # never use the Windows locale codec (cp949) for the prompt/JSON pipe
                capture_output=True,
                cwd=config.cwd,
                env=env,
                timeout=config.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise CodexOperatingAgentError(
                    f"codex exec failed with exit={completed.returncode}: {completed.stderr.strip()}"
                )
            if output_path.exists():
                return output_path.read_text(encoding="utf-8").strip()
            if completed.stdout.strip():
                return completed.stdout.strip()
            raise CodexOperatingAgentError("codex exec produced no final response")


class CodexCliOperatingAgent:
    """Login-session Codex operating agent.

    This adapter intentionally invokes `codex exec` instead of the OpenAI API. Auth is
    delegated to the local Codex installation, so a ChatGPT login stored by `codex login`
    can be used without passing an API key into task_management runtime code.
    """

    def __init__(
        self,
        config: CodexCliOperatingAgentConfig | None = None,
        *,
        runner: CodexExecRunner | None = None,
        fallback_agent: TeamTaskOperatingAgent | None = None,
    ) -> None:
        self.config = config or CodexCliOperatingAgentConfig.from_env()
        self.runner = runner or SubprocessCodexExecRunner()
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
            text = self.runner.run_decision(
                prompt,
                schema=CODEX_DECISION_OUTPUT_SCHEMA,
                config=self.config,
            )
            decision = decision_from_payload(_normalize_codex_payload(_loads_json_object(text)))
            return replace(
                decision,
                source="codex_cli",
                rationale=_with_codex_note(decision.rationale, self.config.model),
            )
        except Exception as exc:  # noqa: BLE001 - boundary catches model/process/schema failures for home-server stability
            if not self.config.fallback_on_error:
                raise CodexOperatingAgentError(str(exc)) from exc
            return replace(
                fallback_decision,
                source="codex_cli_fallback",
                rationale=f"Codex CLI operating agent unavailable or invalid; used rule fallback. reason={exc}",
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
        context["rules"]["auth_policy"] = "Use the local Codex login session only; do not ask for or output API keys."
        return (
            _SYSTEM_PROMPT
            + "\n\nReturn only one JSON object matching the provided output schema.\n"
            + "For proposal_drafts, encode metadata as metadata_json: a JSON object string with string values.\n"
            + "For proposal_patches, encode temporal updates as temporal_update_json: a JSON object string with string values.\n"
            + "Every proposal_patch must include target_confidence, evidence_text, assumptions, missing_slots, and needs_clarification.\n"
            + "For completion/progress/deferral/confirmation/correction feedback, emit proposal_patches rather than new tasks. "
            + "Use temporal_update_json keys status=done, status=confirmed, progress_status=partial|complete, "
            + "progress_percent, remaining_work, completion_scope, and "
            + "semantic_update_type=completion|progress|deferral|confirmation|correction as applicable. "
            + "If only a preparation/material/subtask scope is complete while the parent task/event remains open, "
            + "set completion_scope=preparation|materials|subtask and progress_status=complete; do not complete the parent.\n"
            + "Use correction/title keys only when the user explicitly corrects an existing item: title or corrected_title, "
            + "with semantic_update_type=correction. Use metadata keys the core understands: participants, external_owner, external_participants, participant_label, "
            + "location, location_optional, date_window_start, date_window_end, needs_exact_date, needs_exact_time, materials, needs_prep, "
            + "parent_proposal_id, parent_source_key, depends_on_proposal_ids, depends_on_source_keys, step_index, step_count, workflow_id, workflow_title, "
            + "workflow_role, risk_level, risk_reason, requires_separate_approval.\n"
            + "For study/meeting/event lifecycles, prefer a stable workflow root for the actual named event over narrow planning steps. "
            + "Post-event deliverables such as follow-up materials, completion reports, result sharing, or outbound email attach to the workflow root "
            + "or nearest active workflow ancestor, not to a completed schedule-decision child; keep that decision as a dependency when relevant.\n"
            + "<task_management_context>\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n</task_management_context>\n"
        )


_SYSTEM_PROMPT = build_operating_agent_system_instructions(
    backend_runtime="Codex CLI using the user's local Codex login session",
    source_key_prefix="codex",
    temporal_update_field="proposal_patches.temporal_update_json",
)


CODEX_DECISION_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "action",
        "source",
        "confidence",
        "rationale",
        "proposal_drafts",
        "proposal_patches",
        "clarification_questions",
    ],
    "properties": {
        "schema": {"type": "string", "const": OPERATING_AGENT_SCHEMA},
        "action": {"type": "string", "enum": ["create_proposals", "apply_feedback", "no_action"]},
        "source": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "proposal_drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_key",
                    "raw_text",
                    "title",
                    "discussion_id",
                    "message_id",
                    "line_number",
                    "speaker",
                    "assigned_to",
                    "task_management_area",
                    "due_date",
                    "scheduled_date",
                    "time_window",
                    "task_status",
                    "item_type",
                    "disposition",
                    "needs_review",
                    "source_url",
                    "source_export_path",
                    "metadata_json",
                ],
                "properties": {
                    "source_key": {"type": "string"},
                    "raw_text": {"type": "string"},
                    "title": {"type": "string"},
                    "discussion_id": {"type": "string"},
                    "message_id": {"type": "string"},
                    "line_number": {"type": "integer"},
                    "speaker": {"type": "string"},
                    "assigned_to": {"type": "string", "enum": ["me", "teammate", "shared", "unassigned"]},
                    "task_management_area": {"type": "string"},
                    "due_date": {"type": "string"},
                    "scheduled_date": {"type": "string"},
                    "time_window": {"type": "string"},
                    "task_status": {"type": "string"},
                    "item_type": {"type": "string", "enum": ["task", "event", "routine", "reference", "question", "decision"]},
                    "disposition": {"type": "string"},
                    "needs_review": {"type": "boolean"},
                    "source_url": {"type": "string"},
                    "source_export_path": {"type": "string"},
                    "metadata_json": {"type": "string"},
                },
            },
        },
        "proposal_patches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "request_id",
                    "proposal_id",
                    "actor_id",
                    "body",
                    "temporal_update_json",
                    "reason",
                    "target_confidence",
                    "evidence_text",
                    "assumptions",
                    "missing_slots",
                    "needs_clarification",
                ],
                "properties": {
                    "request_id": {"type": "string"},
                    "proposal_id": {"type": "string"},
                    "actor_id": {"type": "string"},
                    "body": {"type": "string"},
                    "temporal_update_json": {"type": "string"},
                    "reason": {"type": "string"},
                    "target_confidence": {"type": "number"},
                    "evidence_text": {"type": "string"},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "missing_slots": {"type": "array", "items": {"type": "string"}},
                    "needs_clarification": {"type": "boolean"},
                },
            },
        },
        "clarification_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["recipient_id", "prompt", "proposal_id", "missing_slots"],
                "properties": {
                    "recipient_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "proposal_id": {"type": "string"},
                    "missing_slots": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def _loads_json_object(text: str) -> Mapping[str, object]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    payload = json.loads(stripped)
    if not isinstance(payload, Mapping):
        raise CodexOperatingAgentError("Codex output was not a JSON object")
    return payload


def _normalize_codex_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    normalized = dict(payload)
    drafts = []
    for item in payload.get("proposal_drafts", []) or []:
        if not isinstance(item, Mapping):
            drafts.append(item)
            continue
        draft = dict(item)
        if "metadata_json" in draft:
            draft["metadata"] = _json_string_object(draft.pop("metadata_json"), "metadata_json")
        drafts.append(draft)
    patches = []
    for item in payload.get("proposal_patches", []) or []:
        if not isinstance(item, Mapping):
            patches.append(item)
            continue
        patch = dict(item)
        if "temporal_update_json" in patch:
            patch["temporal_update"] = _json_string_object(patch.pop("temporal_update_json"), "temporal_update_json")
        patch.setdefault("target_confidence", 1.0)
        patch.setdefault("evidence_text", str(patch.get("body", "")))
        patch.setdefault("assumptions", [])
        patch.setdefault("missing_slots", [])
        patch.setdefault("needs_clarification", False)
        patches.append(patch)
    normalized["proposal_drafts"] = drafts
    normalized["proposal_patches"] = patches
    return normalized


def _json_string_object(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, str):
        raise CodexOperatingAgentError(f"{label} must be a string")
    payload = json.loads(value or "{}")
    if not isinstance(payload, Mapping):
        raise CodexOperatingAgentError(f"{label} must decode to an object")
    return {str(key): str(item) for key, item in payload.items()}


def _resolve_codex_bin(codex_bin: str) -> str:
    resolved = shutil.which(codex_bin)
    if resolved:
        return resolved
    if os.name == "nt" and not codex_bin.lower().endswith((".cmd", ".exe", ".ps1")):
        for suffix in (".cmd", ".exe"):
            resolved = shutil.which(f"{codex_bin}{suffix}")
            if resolved:
                return resolved
    return codex_bin


def _with_codex_note(rationale: str, model: str) -> str:
    note = "codex_cli_login_session"
    if model:
        note = f"{note}; model={model}"
    return f"{rationale} ({note})" if rationale else note

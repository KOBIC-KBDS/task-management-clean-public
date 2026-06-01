from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from typing import Any, Mapping, Protocol, Sequence
from urllib import request as urlrequest

from .domain import ApprovalRequest, IncomingMessage, Proposal
from .operating_agent import (
    OPERATING_DECISION_OUTPUT_SCHEMA,
    TeamTaskOperatingAgent,
    OperatingAgentDecision,
    RuleBasedTeamTaskOperatingAgent,
    decision_from_payload,
)
from .semantic_context import build_operating_agent_context


class OpenAIOperatingAgentError(RuntimeError):
    """Raised when the optional OpenAI operating-agent adapter cannot produce a valid decision."""


@dataclass(frozen=True)
class OpenAIOperatingAgentConfig:
    api_key: str = ""
    model: str = "gpt-5.5"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 30.0
    fallback_on_error: bool = True
    strict_schema: bool = True

    @classmethod
    def from_env(cls) -> "OpenAIOperatingAgentConfig":
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("TASK_MANAGEMENT_OPENAI_MODEL", "gpt-5.5"),
            base_url=os.environ.get("TASK_MANAGEMENT_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout_seconds=float(os.environ.get("TASK_MANAGEMENT_OPENAI_TIMEOUT_SECONDS", "30")),
            fallback_on_error=os.environ.get("TASK_MANAGEMENT_OPENAI_FALLBACK", "1").lower() not in {"0", "false", "no", "off"},
            strict_schema=os.environ.get("TASK_MANAGEMENT_OPENAI_STRICT_SCHEMA", "1").lower() not in {"0", "false", "no", "off"},
        )


class OpenAIResponsesClient(Protocol):
    def create_response(self, payload: Mapping[str, Any], *, timeout_seconds: float) -> Mapping[str, Any]:
        """Create a Responses API result and return decoded JSON."""


class StdlibOpenAIResponsesClient:
    """Small stdlib Responses API client to avoid adding a hard OpenAI SDK dependency."""

    def __init__(self, *, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        if not api_key:
            raise OpenAIOperatingAgentError("OPENAI_API_KEY is required for the OpenAI operating agent")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def create_response(self, payload: Mapping[str, Any], *, timeout_seconds: float) -> Mapping[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/responses",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:  # nosec - explicit opt-in adapter
            return json.loads(response.read().decode("utf-8"))


class OpenAIResponsesOperatingAgent:
    """Optional LLM operating agent that can only propose strict decisions.

    The deterministic fallback remains available because Slack/home-server polling should not
    stop merely because a model call, network, or schema response fails.
    """

    def __init__(
        self,
        config: OpenAIOperatingAgentConfig | None = None,
        *,
        client: OpenAIResponsesClient | None = None,
        fallback_agent: TeamTaskOperatingAgent | None = None,
    ) -> None:
        self.config = config or OpenAIOperatingAgentConfig.from_env()
        self.client = client
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
            client = self._client()
            request_payload = self._request_payload(
                message,
                pending_approval_requests=pending_approval_requests,
                pending_proposals=pending_proposals,
                fallback_decision=fallback_decision,
            )
            response_payload = client.create_response(request_payload, timeout_seconds=self.config.timeout_seconds)
            decision_payload = json.loads(_extract_output_text(response_payload))
            decision = decision_from_payload(decision_payload)
            return replace(
                decision,
                source="openai_responses",
                rationale=_with_model_note(decision.rationale, self.config.model),
            )
        except Exception as exc:  # noqa: BLE001 - boundary intentionally catches all model/network/schema failures
            if not self.config.fallback_on_error:
                raise OpenAIOperatingAgentError(str(exc)) from exc
            return replace(
                fallback_decision,
                source="rule_based_fallback",
                rationale=f"OpenAI operating agent unavailable or invalid; used rule fallback. reason={exc}",
            )

    def _client(self) -> OpenAIResponsesClient:
        if self.client is not None:
            return self.client
        return StdlibOpenAIResponsesClient(api_key=self.config.api_key, base_url=self.config.base_url)

    def _request_payload(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
        fallback_decision: OperatingAgentDecision,
    ) -> dict[str, Any]:
        context = build_operating_agent_context(
            message,
            pending_approval_requests=pending_approval_requests,
            pending_proposals=pending_proposals,
            fallback_decision=fallback_decision,
        )
        return {
            "model": self.config.model,
            "instructions": _SYSTEM_INSTRUCTIONS,
            "input": json.dumps(context, ensure_ascii=False, sort_keys=True),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "task_management_operating_decision",
                    "schema": OPERATING_DECISION_OUTPUT_SCHEMA,
                    "strict": self.config.strict_schema,
                }
            },
        }


_SYSTEM_INSTRUCTIONS = """You are the operating agent for a task_management task-management system.
Return exactly one JSON object matching task-task_management.operating-agent.v1.
You do not mutate storage, approve proposals, write task-core files, or send calendar/Slack messages.
Your job is only to interpret the current message and emit proposal drafts, proposal patches, or no_action.
The deterministic core will enforce missing slots, approvals, idempotency, audit logs, and preview-only task-core export.
Preserve Korean text as UTF-8. Use concise Korean titles when appropriate.
Use source_key values that are stable for the message, such as agent/<message_id>/1.
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
For follow-up batches tied to a pending or approved review task, preserve parent_proposal_id and inherit the review date/time unless the new message overrides it.
For feedback, resolve the semantic target proposal/request from pending_proposal_cards before filling slots. Treat a message as feedback only when it contains explicit target evidence: a request/proposal id, direct title/semantic handle match, or reply/anaphora wording such as 이 건/방금 말한/그 일정/해당 일정. If the current message introduces a new task/event/routine without that evidence, emit create_proposals even when pending_proposal_cards exist.
If one human reply updates multiple pending proposals, emit multiple proposal_patches.
For completion/progress/deferral/confirmation/correction feedback, emit proposal_patches rather than new tasks. Put status=done and semantic_update_type=completion for full completion; progress_status=partial, progress_percent, remaining_work, and semantic_update_type=progress for partial progress; due_date/scheduled_date/time_window plus semantic_update_type=deferral for deferrals; status=confirmed plus semantic_update_type=confirmation for temporal/slot confirmations that should not complete the proposal; and corrected title or metadata slots such as corrected_title/title, external_owner, external_participants, participant_label, participants, location/location_optional plus semantic_update_type=correction for explicit corrections. Corrections must not set status. If only preparation/materials/subtask work is complete while the parent task/event remains open, include status=done, progress_status=complete, completion_scope=preparation|materials|subtask, semantic_update_type=completion so core records progress rather than closing the parent.
If target confidence is below 0.65 or the target is ambiguous, set needs_clarification=true and ask for clarification rather than guessing.
Every proposal_patch must include target_confidence, evidence_text, assumptions, missing_slots, and needs_clarification.
If the deterministic_baseline is sufficient, you may return an equivalent decision with improved title/metadata only.
"""


def _extract_output_text(response_payload: Mapping[str, Any]) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    for item in response_payload.get("output", []) or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, Mapping):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise OpenAIOperatingAgentError("OpenAI response did not include output text")


def _with_model_note(rationale: str, model: str) -> str:
    note = f"model={model}"
    return f"{rationale} ({note})" if rationale else note

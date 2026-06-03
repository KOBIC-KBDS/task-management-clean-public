from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Sequence

from .domain import (
    ASSIGNEE_VALUES,
    PROPOSAL_KIND_VALUES,
    ApprovalRequest,
    IncomingMessage,
    Proposal,
)
from .human_view import date_label
from .operating_agent import OPERATING_AGENT_SCHEMA, OperatingAgentDecision


def build_operating_agent_context(
    message: IncomingMessage,
    *,
    pending_approval_requests: Sequence[ApprovalRequest],
    pending_proposals: Sequence[Proposal],
    fallback_decision: OperatingAgentDecision,
) -> dict[str, Any]:
    """Return the standardized semantic context pack for LLM operating agents.

    The pack intentionally contains both raw persisted objects and compact human
    cards.  Raw objects preserve lossless ids/state for strict patching; cards
    make target selection easy and explicit for the LLM.
    """

    pending_requests = tuple(pending_approval_requests)
    request_ids_by_proposal = _request_ids_by_proposal(pending_requests)
    message_payload = _json_safe(message)
    recent_conversation = message_payload.pop("recent_conversation", [])
    return {
        "schema": OPERATING_AGENT_SCHEMA,
        "semantic_contract": {
            "north_star": "LLM reads human message plus active state context and emits strict proposal drafts or targeted patches.",
            "agent_role": "semantic_interpreter_only",
            "core_role": "validate_schema_apply_policy_persist_state_render_messages_preview_only_task_core",
            "must_resolve_target_before_slots": True,
            "multi_patch_allowed": True,
            "low_confidence_policy": "ask_clarification_do_not_mutate",
        },
        "today": message.received_at.date().isoformat(),
        "timezone": "Asia/Seoul",
        "message": message_payload,
        "recent_conversation": recent_conversation,
        "pending_approval_requests": [_json_safe(item) for item in pending_requests],
        "pending_proposals": [_json_safe(item) for item in pending_proposals],
        "pending_proposal_cards": [
            _proposal_card(proposal, request_ids=request_ids_by_proposal.get(proposal.proposal_id, ()))
            for proposal in pending_proposals
        ],
        "deterministic_baseline": fallback_decision.to_payload(),
        "rules": {
            "agent_role": "interpret task_management messages and propose next actions only",
            "state_owner": "TeamTaskOrchestrator/core commits all SQLite, approval, export, and calendar state changes",
            "allowed_actions": ["create_proposals", "apply_feedback", "no_action"],
            "actors": list(ASSIGNEE_VALUES),
            "allowed_item_types": list(PROPOSAL_KIND_VALUES),
            "slack_notification_policy": (
                "If message.visibility=team and message_id starts with slack/, treat it as an allowlisted Slack "
                "notification, not as a private command. Emit create_proposals only when the text contains a clear "
                "actionable request or commitment for the configured user; otherwise emit no_action. Do not apply "
                "feedback patches from Slack notification messages. The core will require user confirmation before "
                "any notification-derived proposal is approved."
            ),
            "no_action_policy": (
                "DM and channel differ by nature. A private DM (visibility=private) is the user speaking directly "
                "to the bot, so it must never be silently dropped: always emit create_proposals when there is a "
                "task/event/routine/commitment, otherwise set clarification_questions to confirm intent — including "
                "whether a casually phrased self-plan (하하 ... 저녁 먹을거야), a time, or an activity should be tracked. "
                "Reserve no_action in a DM for a pure greeting/acknowledgement only; when unsure, ask rather than "
                "stay silent. A team-channel message (visibility=team), by contrast, may not target the bot or user "
                "at all, so letting it pass with no_action is acceptable there, subject to slack_notification_policy "
                "(mention-required / allowlist)."
            ),
            "date_fields": "Use ISO YYYY-MM-DD or empty string. Do not invent exact dates when a window is ambiguous.",
            "approval_policy": "Do not mark approved in agent output; core decides approval requests and statuses.",
            "time_policy": "For broad time hints like 점심/오전/오후/저녁, keep the broad hint and set needs_exact_time when exact time is required.",
            "item_type_policy": (
                "Do not invent new item_type values. If a requested category does not fit task/event/routine/"
                "reference/question/decision, ask clarification or emit a decision item with "
                "metadata.type_policy_needed=true and metadata.type_request set to the requested label."
            ),
            "target_policy": (
                "For feedback, choose target proposal_id/request_id from pending_proposal_cards before filling slots. "
                "If multiple targets are referenced, emit multiple proposal_patches. "
                "If target confidence is below 0.65, set needs_clarification=true instead of guessing."
            ),
            "patch_evidence_policy": "Each proposal patch must include target_confidence, evidence_text, assumptions, and missing_slots.",
            "semantic_update_types": "Use only completion, progress, deferral, confirmation, or correction. Use correction for explicit title/metadata/slot corrections without changing status.",
            "external_counterpart_policy": (
                "External work counterparts named as 담당자 are not task_management assignees. "
                "Use assigned_to=me/teammate/shared for the internal owner, and preserve named counterparts in "
                "metadata.external_owner, metadata.external_participants, metadata.participant_label, and participants."
            ),
        },
    }


def _proposal_card(proposal: Proposal, *, request_ids: tuple[str, ...]) -> dict[str, Any]:
    proposal_date = proposal.scheduled_date or proposal.due_date
    return {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "raw_text": proposal.raw_text,
        "kind": proposal.kind,
        "status": proposal.status,
        "assigned_to": proposal.assigned_to,
        "missing_slots": list(proposal.missing_slots),
        "required_approvers": list(proposal.required_approvers),
        "approvals": list(proposal.approvals),
        "pending_request_ids": list(request_ids),
        "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
        "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
        "date_label": date_label(proposal_date) if proposal_date else "",
        "time_window": proposal.time_window,
        "task_management_area": proposal.task_management_area,
        "source_message_id": proposal.source_message_id,
        "metadata": _important_metadata(proposal.metadata),
        "semantic_handles": _semantic_handles(proposal),
    }


def _request_ids_by_proposal(requests: Sequence[ApprovalRequest]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for request in requests:
        if request.status != "pending":
            continue
        grouped.setdefault(request.proposal_id, []).append(request.request_id)
    return {proposal_id: tuple(ids) for proposal_id, ids in grouped.items()}


def _semantic_handles(proposal: Proposal) -> list[str]:
    handles = [proposal.title]
    for key in (
        "parent_proposal_id",
        "parent_source_key",
        "depends_on_proposal_ids",
        "depends_on_proposal_id",
        "depends_on_source_keys",
        "step_index",
        "step_count",
        "workflow_id",
        "workflow_title",
        "workflow_role",
        "risk_level",
        "risk_reason",
        "requires_separate_approval",
        "conflict_with_proposal_ids",
        "source_ts",
    ):
        value = proposal.metadata.get(key)
        if value:
            handles.append(f"{key}:{value}")
    for token in ("차주", "복귀", "출장", "회의", "회식", "준비", "확인", "정리"):
        if token in f"{proposal.title} {proposal.raw_text}":
            handles.append(token)
    return list(dict.fromkeys(handles))


def _important_metadata(metadata: dict[str, str]) -> dict[str, str]:
    important_keys = (
        "participants",
        "external_participants",
        "participant_label",
        "attendees",
        "location",
        "location_optional",
        "date_window_start",
        "date_window_end",
        "date_window_label",
        "needs_exact_date",
        "needs_exact_time",
        "deferred_missing_slots",
        "deferred_until",
        "conflict_detected",
        "conflict_with_proposal_ids",
        "event_scope",
        "blocks_in_person",
        "parent_proposal_id",
        "parent_source_key",
        "depends_on_proposal_ids",
        "depends_on_proposal_id",
        "depends_on_source_keys",
        "step_index",
        "step_count",
        "workflow_id",
        "workflow_title",
        "workflow_role",
        "risk_level",
        "risk_reason",
        "requires_separate_approval",
        "link_type",
        "internal_owner",
        "external_owner",
        "collaboration_context",
        "parent_title",
        "context_inherited_date",
        "context_inherited_from",
    )
    return {key: metadata[key] for key in important_keys if metadata.get(key)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return value

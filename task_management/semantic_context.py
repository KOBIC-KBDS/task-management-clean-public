from __future__ import annotations

from .relations import (
    ATTENDEES_KEY,
    CONFLICT_DETECTED_KEY,
    CONFLICT_WITH_PROPOSAL_IDS_KEY,
    DATE_WINDOW_END_KEY,
    DATE_WINDOW_LABEL_KEY,
    DATE_WINDOW_START_KEY,
    DEFERRED_MISSING_SLOTS_KEY,
    DEFERRED_UNTIL_KEY,
    EXTERNAL_PARTICIPANTS_KEY,
    LINK_TYPE_KEY,
    LOCATION_KEY,
    LOCATION_OPTIONAL_KEY,
    NEEDS_EXACT_TIME_KEY,
    PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
    SOURCE_TS_KEY,
)

from dataclasses import asdict
from datetime import date, datetime
import re
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
    context_proposals = _select_context_proposals(
        message,
        tuple(pending_proposals),
        pending_requests=pending_requests,
    )
    index_proposals = _select_index_proposals(tuple(pending_proposals), context_proposals)
    message_payload = _json_safe(message)
    recent_conversation = message_payload.pop("recent_conversation", [])
    return {
        "schema": OPERATING_AGENT_SCHEMA,
        "semantic_contract": {
            "north_star": "LLM reads human message plus active state context and emits strict proposals, targeted patches, or read-only answers.",
            "agent_role": "semantic_interpreter_only",
            "core_role": "validate_schema_apply_policy_persist_state_render_messages_preview_only_task_core",
            "must_resolve_target_before_slots": True,
            "multi_patch_allowed": True,
            "mixed_response_and_mutation_allowed": True,
            "low_confidence_policy": "ask_clarification_do_not_mutate",
        },
        "today": message.received_at.date().isoformat(),
        "timezone": "Asia/Seoul",
        "message": message_payload,
        "recent_conversation": recent_conversation,
        "pending_approval_requests": [_json_safe(item) for item in pending_requests],
        "proposal_counts": {
            "all": len(pending_proposals),
            "indexed": len(index_proposals),
            "detailed_context": len(context_proposals),
        },
        "proposal_index": [_proposal_index_entry(proposal) for proposal in index_proposals],
        "pending_proposals": [_proposal_index_entry(item) for item in context_proposals],
        "pending_proposal_cards": [
            _proposal_card(proposal, request_ids=request_ids_by_proposal.get(proposal.proposal_id, ()))
            for proposal in context_proposals
        ],
        "deterministic_baseline": fallback_decision.to_payload(),
        "rules": {
            "agent_role": "interpret task_management messages and propose next actions only",
            "state_owner": "TeamTaskOrchestrator/core commits all SQLite, approval, export, and calendar state changes",
            "allowed_actions": ["create_proposals", "apply_feedback", "respond", "no_action"],
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
                "to the bot, so it must never be silently dropped: emit create_proposals when there is a "
                "task/event/routine/commitment, use direct_responses for useful ordinary conversation and read-only "
                "questions, otherwise set "
                "clarification_questions to confirm intent — including "
                "whether a casually phrased self-plan (하하 ... 저녁 먹을거야), a time, or an activity should be tracked. "
                "A greeting or acknowledgement may receive a brief conversational response when useful. Reserve "
                "no_action in a DM for duplicate/system noise or when a reply clearly adds no value; when unsure, "
                "ask rather than stay silent. A team-channel message (visibility=team), by contrast, may not target the bot or user "
                "at all, so letting it pass with no_action is acceptable there, subject to slack_notification_policy "
                "(mention-required / allowlist)."
            ),
            "direct_response_policy": (
                "Use direct_responses for useful private-DM conversation about tasks, planning, app behavior, draft "
                "wording, existing item/status/approval/workflow meaning, reasons, guidance, explanation, display, or "
                "summaries without changing proposal or approval state. Use action=respond when no mutation is requested. The same "
                "decision may include direct_responses plus drafts/patches for mixed intent. Attach exact proposal_id/"
                "request_id when the answer concerns an existing item and use recipient_id=current sender. The [요청] "
                "tag is optional: set interaction_label=request only when the literal tag is present; otherwise leave it "
                "empty for a normal conversational reply. Never put [요청] in persisted task titles/types. "
                "Do not turn an explanation into a needs_clarification patch and do not claim an external action ran."
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
                "For feedback, choose exact proposal_id/request_id from proposal_index and pending_proposal_cards before filling slots. "
                "If multiple targets are referenced, emit multiple proposal_patches. "
                "If target confidence is below 0.65, set needs_clarification=true instead of guessing."
            ),
            "workflow_restructure_policy": (
                "Private-DM instructions may reorganize existing workflow state. Emit one apply_feedback patch targeting "
                "the exact current parent with semantic_update_type=workflow_restructure. For standalone children use "
                "relation_action=detach_children and child_proposal_ids=<comma-separated exact direct-child ids>. Add "
                "status=done only when the user explicitly asks to complete the old parent. The current safe contract "
                "supports leaf-child detachment; ask clarification for reparenting or nested-workflow moves. Never invent "
                "ids; ask clarification when target or children are ambiguous."
            ),
            "duplicate_merge_policy": (
                "When the user explicitly says two existing items are the same or asks to merge them, emit one "
                "apply_feedback patch with semantic_update_type=duplicate_merge. The patch proposal_id is the "
                "duplicate/source item, request_id is its pending request when present, and "
                "merge_target_proposal_id is the exact canonical target id from proposal_index/cards. Include the "
                "desired final title or corrected slots when stated. Never encode a merge as a correction or "
                "confirmation that leaves conflict_resolution pending."
            ),
            "workflow_root_policy": (
                "For study/meeting/event lifecycles, treat review discussions, schedule decisions, and prep meetings "
                "as steps under a stable workflow root for the actual named event when that root can be inferred. "
                "Post-event deliverables such as follow-up materials, completion reports, result sharing, or outbound "
                "email belong under the workflow root or nearest active workflow ancestor, not under a completed "
                "schedule-decision child; keep the completed decision as a dependency when relevant."
            ),
            "patch_evidence_policy": "Each proposal patch must include target_confidence, evidence_text, assumptions, and missing_slots.",
            "semantic_update_types": (
                "Use completion, progress, deferral, confirmation, correction, duplicate_merge, or workflow_restructure. Use correction "
                "for explicit title/metadata/slot corrections without changing status; use workflow_restructure only "
                "for validated changes to existing parent/child relations."
            ),
            "external_counterpart_policy": (
                "External work counterparts named as 담당자 are not task_management assignees. "
                "Use assigned_to=me/teammate/shared for the internal owner, and preserve named counterparts in "
                "metadata.external_owner, metadata.external_participants, metadata.participant_label, and participants."
            ),
        },
    }


_MAX_DETAILED_CONTEXT_PROPOSALS = 16
_MAX_INDEX_PROPOSALS = 64
_GENERIC_CONTEXT_TOKENS = frozenset(
    {
        "작업",
        "업무",
        "일정",
        "완료",
        "처리",
        "하위",
        "상위",
        "별개",
        "독립",
        "분리",
        "변경",
        "수정",
        "해줘",
        "해주세요",
    }
)


def _select_context_proposals(
    message: IncomingMessage,
    proposals: tuple[Proposal, ...],
    *,
    pending_requests: tuple[ApprovalRequest, ...],
) -> tuple[Proposal, ...]:
    """Keep semantic context broad in coverage but bounded in detail.

    The compact ``proposal_index`` still exposes every exact id/title/relation.
    Full raw proposal objects and cards are limited to pending requests, strong
    lexical candidates, their parent/children/siblings, and recent active work.
    This avoids sending the same large metadata corpus twice to a CLI agent.
    """

    if len(proposals) <= _MAX_DETAILED_CONTEXT_PROPOSALS:
        return proposals

    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    children_by_parent: dict[str, list[Proposal]] = {}
    for proposal in proposals:
        parent_id = proposal.metadata.get("parent_proposal_id", "").strip()
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(proposal)

    selected_ids = {
        request.proposal_id
        for request in pending_requests
        if request.status == "pending" and request.proposal_id in by_id
    }
    message_tokens = _context_tokens(message.text)
    scored: list[tuple[int, datetime, str, Proposal]] = []
    compact_message = re.sub(r"\s+", "", message.text).lower()
    for proposal in proposals:
        title_tokens = _context_tokens(proposal.title)
        relation_text = " ".join(
            (
                proposal.metadata.get("workflow_title", ""),
                proposal.metadata.get("parent_title", ""),
                proposal.metadata.get("materials", ""),
            )
        )
        context_tokens = title_tokens | _context_tokens(proposal.raw_text) | _context_tokens(relation_text)
        shared_title = message_tokens & title_tokens
        shared_context = message_tokens & context_tokens
        compact_title = re.sub(r"\s+", "", proposal.title).lower()
        score = 12 * len(shared_title) + 4 * len(shared_context - shared_title)
        if compact_title and compact_title in compact_message:
            score += 60
        if score:
            scored.append((score, proposal.updated_at, proposal.proposal_id, proposal))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    selected_ids.update(item[3].proposal_id for item in scored[:24])
    focus_ids = set(selected_ids)

    relation_seed_ids = tuple(selected_ids)
    for proposal_id in relation_seed_ids:
        proposal = by_id.get(proposal_id)
        if proposal is None:
            continue
        parent_id = proposal.metadata.get("parent_proposal_id", "").strip()
        if parent_id in by_id:
            selected_ids.add(parent_id)
            selected_ids.update(child.proposal_id for child in children_by_parent.get(parent_id, ()))
        selected_ids.update(child.proposal_id for child in children_by_parent.get(proposal_id, ()))

    prioritized = sorted(
        (by_id[proposal_id] for proposal_id in selected_ids if proposal_id in by_id),
        key=lambda proposal: (
            proposal.proposal_id not in focus_ids,
            proposal.status in {"done", "rejected"},
            -proposal.updated_at.timestamp(),
            proposal.proposal_id,
        ),
    )
    if len(prioritized) < _MAX_DETAILED_CONTEXT_PROPOSALS:
        recent_active = sorted(
            (
                proposal
                for proposal in proposals
                if proposal.proposal_id not in selected_ids and proposal.status not in {"done", "rejected"}
            ),
            key=lambda proposal: (proposal.updated_at, proposal.proposal_id),
            reverse=True,
        )
        prioritized.extend(recent_active[: _MAX_DETAILED_CONTEXT_PROPOSALS - len(prioritized)])
    return tuple(prioritized[:_MAX_DETAILED_CONTEXT_PROPOSALS])


def _proposal_index_entry(proposal: Proposal) -> dict[str, str]:
    return {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "kind": proposal.kind,
        "status": proposal.status,
        "parent_proposal_id": proposal.metadata.get("parent_proposal_id", ""),
        "workflow_title": proposal.metadata.get("workflow_title", ""),
        "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
        "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
        "time_window": proposal.time_window,
    }


def _select_index_proposals(
    proposals: tuple[Proposal, ...],
    detailed: tuple[Proposal, ...],
) -> tuple[Proposal, ...]:
    if len(proposals) <= _MAX_INDEX_PROPOSALS:
        return proposals
    selected = list(detailed)
    selected_ids = {proposal.proposal_id for proposal in selected}
    remaining = sorted(
        (proposal for proposal in proposals if proposal.proposal_id not in selected_ids),
        key=lambda proposal: (
            proposal.status in {"done", "rejected"},
            -proposal.updated_at.timestamp(),
            proposal.proposal_id,
        ),
    )
    selected.extend(remaining[: _MAX_INDEX_PROPOSALS - len(selected)])
    return tuple(selected)


def _context_tokens(text: str) -> set[str]:
    tokens = {
        token
        for token in re.split(r"[^0-9A-Za-z가-힣/]+", text.lower())
        if len(token) >= 2 and token not in _GENERIC_CONTEXT_TOKENS
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(part for part in token.split("/") if len(part) >= 2)
    return expanded


def recent_conversation_from_events(
    events,
    *,
    chat_id: str,
    sender_id: str,
    current_message: IncomingMessage | None = None,
    limit: int = 8,
    max_chars: int = 600,
) -> tuple[dict, ...]:
    """Reconstruct recent DM turns (user + bot) from the audit log.

    Gives the semantic agent the prior conversation so it can resolve a reply
    against context (e.g. an affirmative answer to the bot's own pending
    question) instead of guessing the target. Pure context, not a rule.

    When ``current_message`` is provided, the matching ``message.received``
    event is skipped during the scan so the message never contextualizes
    itself, and the current message is appended as the trailing user turn.
    """

    exclude_message_id = current_message.message_id if current_message is not None else None
    turns: list[dict] = []
    for event in events:
        etype = event.get("type")
        payload = event.get("payload") or {}
        msg = payload.get("message") or {}
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        if etype == "message.received" and msg.get("chat_id") == chat_id:
            if exclude_message_id is not None and msg.get("message_id") == exclude_message_id:
                continue
            turns.append({"role": "user", "text": text[:max_chars]})
        elif etype == "slack.message.sent" and payload.get("recipient_id") == sender_id:
            turns.append({"role": "assistant", "text": text[:max_chars]})
    if current_message is not None:
        current_text = str(current_message.text or "").strip()
        if current_text:
            turns.append({"role": "user", "text": current_text[:max_chars]})
    return tuple(turns[-limit:])


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
        CONFLICT_WITH_PROPOSAL_IDS_KEY,
        SOURCE_TS_KEY,
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
        PARTICIPANTS_KEY,
        EXTERNAL_PARTICIPANTS_KEY,
        PARTICIPANT_LABEL_KEY,
        ATTENDEES_KEY,
        LOCATION_KEY,
        LOCATION_OPTIONAL_KEY,
        DATE_WINDOW_START_KEY,
        DATE_WINDOW_END_KEY,
        DATE_WINDOW_LABEL_KEY,
        "needs_exact_date",
        NEEDS_EXACT_TIME_KEY,
        DEFERRED_MISSING_SLOTS_KEY,
        DEFERRED_UNTIL_KEY,
        CONFLICT_DETECTED_KEY,
        CONFLICT_WITH_PROPOSAL_IDS_KEY,
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
        LINK_TYPE_KEY,
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

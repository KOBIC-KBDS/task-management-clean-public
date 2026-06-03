from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib

from .domain import ApprovalRequest, OutboundMessage, Proposal
from .human_view import (
    build_missing_slot_question,
    date_label,
    date_window_display_label,
    render_confirmed_sentence,
    render_missing_slot_labels,
    render_missing_slot_sentence,
)
from .slot_validator import missing_slots_for_proposal
from .relations import (
    REQUIRES_SEPARATE_APPROVAL_KEY,
    RISK_LEVEL_KEY,
    STEP_COUNT_KEY,
    STEP_INDEX_KEY,
    WORKFLOW_GROUP_CHILD_IDS_KEY,
    WORKFLOW_GROUP_ID_KEY,
    WORKFLOW_GROUP_REQUEST_ID_KEY,
    WORKFLOW_SEPARATE_CHILD_IDS_KEY,
    requires_separate_approval,
    step_label,
    workflow_group_id,
)


DEFAULT_APPROVERS = ("me", "teammate")


def apply_initial_policy(
    proposal: Proposal,
    *,
    now: datetime,
) -> tuple[Proposal, tuple[ApprovalRequest, ...], tuple[OutboundMessage, ...]]:
    """Apply MVP approval policy to a newly created proposal."""

    if proposal.metadata.get("intake_policy") == "user_confirmation_required":
        approver = proposal.proposer_id or "me"
        awaiting = replace(
            proposal,
            kind="question" if proposal.missing_slots else proposal.kind,
            status="awaiting_approval",
            required_approvers=(approver,),
            approvals=(),
            updated_at=now,
            metadata={
                **proposal.metadata,
                "confirmation_required": "true",
                "confirmation_reason": proposal.metadata.get("intake_policy_reason", "external_notification"),
            },
        )
        request = approval_request(awaiting.proposal_id, approver, now=now)
        return awaiting, (request,), (
            approval_message(
                awaiting,
                request,
                text=render_confirmation_required_sentence(awaiting, request_id=request.request_id),
            ),
        )

    if proposal.kind == "reference":
        approved = replace(proposal, status="approved", approvals=(), required_approvers=(), updated_at=now)
        return approved, (), (
            team_message(
                approved,
                message_type="reference_posted",
                text=f"참고 링크를 등록 후보로 정리했습니다: {approved.title}",
            ),
        )

    if proposal.missing_slots:
        approvers = (proposal.proposer_id,)
        awaiting = replace(
            proposal,
            kind="question",
            status="awaiting_approval",
            required_approvers=approvers,
            updated_at=now,
        )
        request = approval_request(awaiting.proposal_id, awaiting.proposer_id, now=now)
        return awaiting, (request,), (
            missing_slot_question_message(awaiting, request),
        )

    if proposal.assigned_to == "shared":
        approvers = DEFAULT_APPROVERS
        auto_approvals = (proposal.proposer_id,) if proposal.proposer_id in approvers else ()
        awaiting = replace(
            proposal,
            status="awaiting_approval",
            required_approvers=approvers,
            approvals=auto_approvals,
            updated_at=now,
        )
        pending = tuple(actor_id for actor_id in approvers if actor_id not in auto_approvals)
        requests = tuple(approval_request(awaiting.proposal_id, actor_id, now=now) for actor_id in pending)
        return awaiting, requests, tuple(
            approval_message(awaiting, request, text=f"공동 할 일 승인이 필요합니다: {awaiting.title}")
            for request in requests
        )

    if proposal.assigned_to == proposal.proposer_id:
        approved = replace(
            proposal,
            status="approved",
            required_approvers=(proposal.proposer_id,),
            approvals=(proposal.proposer_id,),
            updated_at=now,
        )
        return approved, (), (
            team_message(
                approved,
                message_type="proposal_approved",
                text=render_confirmed_sentence(approved, include_short_id=False),
            ),
        )

    awaiting = replace(
        proposal,
        status="awaiting_approval",
        required_approvers=(proposal.assigned_to,),
        updated_at=now,
    )
    request = approval_request(awaiting.proposal_id, awaiting.assigned_to, now=now)
    return awaiting, (request,), (
        approval_message(awaiting, request, text=f"담당 수락이 필요합니다: {awaiting.title}"),
    )


def apply_assignment_policy(
    proposal: Proposal,
    *,
    actor_id: str,
    now: datetime,
) -> tuple[Proposal, tuple[ApprovalRequest, ...], tuple[OutboundMessage, ...]]:
    """Re-evaluate approval policy after a proposal is reassigned."""

    missing_slots = missing_slots_for_proposal(proposal)
    if missing_slots:
        updated = replace(
            proposal,
            kind="question",
            status="awaiting_approval",
            missing_slots=missing_slots,
            required_approvers=(actor_id,),
            approvals=(),
            updated_at=now,
        )
        request = approval_request(updated.proposal_id, actor_id, now=now)
        return updated, (request,), (
            missing_slot_question_message(updated, request),
        )

    if proposal.assigned_to == "shared":
        approvers = DEFAULT_APPROVERS
        approvals = (actor_id,) if actor_id in approvers else ()
        status = "approved" if set(approvers).issubset(approvals) else "awaiting_approval"
        updated = replace(
            proposal,
            status=status,
            required_approvers=approvers,
            approvals=approvals,
            missing_slots=(),
            updated_at=now,
        )
        pending = tuple(item for item in approvers if item not in approvals)
        requests = tuple(approval_request(updated.proposal_id, item, now=now) for item in pending)
        return updated, requests, tuple(
            approval_message(updated, request, text=f"공동 할 일 승인이 필요합니다: {updated.title}")
            for request in requests
        )

    if proposal.assigned_to == actor_id:
        updated = replace(
            proposal,
            status="approved",
            required_approvers=(actor_id,),
            approvals=(actor_id,),
            missing_slots=(),
            updated_at=now,
        )
        return updated, (), (
            team_message(
                updated,
                message_type="proposal_approved",
                text=render_confirmed_sentence(updated, include_short_id=False),
            ),
        )

    updated = replace(
        proposal,
        status="awaiting_approval",
        required_approvers=(proposal.assigned_to,),
        approvals=(),
        missing_slots=(),
        updated_at=now,
    )
    request = approval_request(updated.proposal_id, updated.assigned_to, now=now)
    return updated, (request,), (
        approval_message(updated, request, text=f"담당 수락이 필요합니다: {updated.title}"),
    )


def approval_request(proposal_id: str, approver_id: str, *, now: datetime) -> ApprovalRequest:
    digest = hashlib.sha1(f"{proposal_id}:{approver_id}".encode("utf-8")).hexdigest()[:12]
    return ApprovalRequest(
        request_id=f"approval/{digest}",
        proposal_id=proposal_id,
        approver_id=approver_id,
        requested_at=now,
    )


def approval_message(proposal: Proposal, request: ApprovalRequest, *, text: str) -> OutboundMessage:
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=request.approver_id,
        message_type="approval_request",
        text=text,
        proposal_id=proposal.proposal_id,
        approval_request_id=request.request_id,
        card=proposal_card(proposal),
    )


def workflow_group_approval_message(
    parent: Proposal,
    request: ApprovalRequest,
    *,
    children: tuple[Proposal, ...],
    separate_children: tuple[Proposal, ...] = (),
) -> OutboundMessage:
    child_lines = []
    for child in children:
        label = step_label(child)
        prefix = f"{label} " if label else ""
        child_lines.append(f"- {prefix}{child.title}")
    separate_lines = []
    for child in separate_children:
        label = step_label(child)
        prefix = f"{label} " if label else ""
        reason = child.metadata.get(RISK_LEVEL_KEY, "") or child.metadata.get("risk_reason", "") or "separate approval"
        separate_lines.append(f"- {prefix}{child.title} ({reason})")
    text_lines = [
        f"연속 작업 묶음으로 보입니다. 한 번에 등록할까요: {parent.title}",
        "(순서대로 이어지는 작업들을 하나의 워크플로우로 묶어 한 번에 등록합니다. 수락하면 각 단계가 하위작업으로 만들어지고, 위험 단계는 따로 확인합니다.)",
        "",
        *child_lines,
    ]
    if separate_lines:
        text_lines.extend(["", "별도 확인이 필요한 단계:", *separate_lines])
    text_lines.extend(
        [
            "",
            f"등록하려면 `수락 {request.request_id}` 또는 `변경 {request.request_id} ...`로 답해주세요.",
            f"아니면 `거절 {request.request_id}`로 전체 묶음을 보류할 수 있습니다.",
        ]
    )
    card = proposal_card(parent)
    card.update(
        {
            WORKFLOW_GROUP_ID_KEY: workflow_group_id(parent),
            WORKFLOW_GROUP_REQUEST_ID_KEY: request.request_id,
            WORKFLOW_GROUP_CHILD_IDS_KEY: ",".join(child.proposal_id for child in children),
            WORKFLOW_SEPARATE_CHILD_IDS_KEY: ",".join(child.proposal_id for child in separate_children),
            "dedupe_key": f"workflow-approval/{workflow_group_id(parent)}/{request.request_id}",
        }
    )
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=request.approver_id,
        message_type="workflow_group_approval_request",
        text="\n".join(text_lines).strip(),
        proposal_id=parent.proposal_id,
        approval_request_id=request.request_id,
        card=card,
    )


def missing_slot_question_message(
    proposal: Proposal,
    request: ApprovalRequest,
    *,
    message_type: str = "approval_request",
    dedupe_key: str = "",
) -> OutboundMessage:
    """Render the canonical human question for proposal slots that are still missing."""

    question = build_missing_slot_question(proposal, request_id=request.request_id)
    message = approval_message(
        proposal,
        request,
        text=render_missing_slot_sentence(question),
    )
    if message_type == message.message_type and not dedupe_key:
        return message
    card = dict(message.card)
    if dedupe_key:
        card["dedupe_key"] = dedupe_key
    return replace(message, message_type=message_type, card=card)


def team_message(proposal: Proposal, *, message_type: str, text: str) -> OutboundMessage:
    return OutboundMessage(
        surface="team_room",
        recipient_id="team",
        message_type=message_type,
        text=text,
        proposal_id=proposal.proposal_id,
        card=proposal_card(proposal),
    )


def proposal_card(proposal: Proposal) -> dict[str, str]:
    date_hint = ""
    if proposal.due_date is not None:
        date_hint = date_label(proposal.due_date)
    elif proposal.scheduled_date is not None:
        date_hint = date_label(proposal.scheduled_date)
    card = {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "kind": proposal.kind,
        "status": proposal.status,
        "assigned_to": proposal.assigned_to,
        "task_management_area": proposal.task_management_area,
        "date": date_hint,
        "date_window": date_window_label(proposal),
        "missing_slots": ",".join(proposal.missing_slots),
        "missing_slot_labels": render_missing_slot_labels(proposal.missing_slots),
        "participants": proposal.metadata.get("participants", ""),
        "location": proposal.metadata.get("location", ""),
        "recurrence": proposal.metadata.get("recurrence_frequency", ""),
    }
    for key in (
        "parent_proposal_id",
        "depends_on_proposal_ids",
        STEP_INDEX_KEY,
        STEP_COUNT_KEY,
        "workflow_id",
        "workflow_title",
        WORKFLOW_GROUP_ID_KEY,
        WORKFLOW_GROUP_REQUEST_ID_KEY,
        WORKFLOW_GROUP_CHILD_IDS_KEY,
        WORKFLOW_SEPARATE_CHILD_IDS_KEY,
        REQUIRES_SEPARATE_APPROVAL_KEY,
        RISK_LEVEL_KEY,
        "risk_reason",
    ):
        if proposal.metadata.get(key):
            card[key] = proposal.metadata[key]
    if requires_separate_approval(proposal):
        card[REQUIRES_SEPARATE_APPROVAL_KEY] = "true"
    return card


def date_window_label(proposal: Proposal) -> str:
    start = proposal.metadata.get("date_window_start", "")
    end = proposal.metadata.get("date_window_end", "")
    label = proposal.metadata.get("date_window_label", "")
    return date_window_display_label(start, end, label)


def render_confirmation_required_sentence(proposal: Proposal, *, request_id: str) -> str:
    source = "Slack 알림" if proposal.metadata.get("source_provider") == "slack" else "외부 알림"
    missing = (
        f"\n필요한 정보: {render_missing_slot_labels(proposal.missing_slots)}"
        if proposal.missing_slots
        else ""
    )
    hint = (
        f"\n등록하려면 `수락 {request_id}` 또는 `변경 {request_id} ...`로 답해주세요. "
        f"아니면 `거절 {request_id}`로 무시할 수 있습니다."
    )
    return f"{source}에서 task 후보로 보입니다. 등록할까요: {proposal.title}{missing}{hint}"

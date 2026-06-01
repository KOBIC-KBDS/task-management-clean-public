from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Iterable

from .approval_policy import approval_request, missing_slot_question_message
from .domain import ApprovalRequest, OutboundMessage, Proposal
from .store import TeamTaskStore


def build_pending_missing_info_followups(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    exclude_proposal_ids: Iterable[str] = (),
) -> tuple[ApprovalRequest, ...]:
    """Ensure old awaiting/missing proposals still have an actionable human prompt.

    Initial proposal intake already sends an approval request.  This sweep is for
    stale or repaired live state: items that are still waiting on slots but either
    lost their pending request or changed after the original prompt text was sent.
    """

    excluded = set(exclude_proposal_ids)
    touched: list[ApprovalRequest] = []
    for proposal in sorted(
        store.list_proposals(),
        key=lambda item: ((item.updated_at or item.created_at or now), item.proposal_id),
    ):
        if proposal.proposal_id in excluded:
            continue
        if not _needs_missing_info_prompt(proposal):
            continue

        request, created = _pending_request_for_proposal(store, proposal, actor_id=actor_id, now=now)
        if created:
            store.save_approval_request(request)
            store.append_event(
                "approval.requested",
                {"request": request, "source": "pending_missing_info_sweep"},
                occurred_at=now,
            )
            touched.append(request)
        elif _current_initial_prompt_was_delivered(store, proposal, request):
            continue

        dedupe_key = _followup_dedupe_key(proposal, request)
        if store.has_outbound_delivery(dedupe_key):
            continue
        message = missing_slot_question_message(
            proposal,
            request,
            message_type="missing_info_followup",
            dedupe_key=dedupe_key,
        )
        queued = store.enqueue_outbound_message(
            dedupe_key=dedupe_key,
            provider="slack",
            surface=message.surface,
            recipient_id=message.recipient_id,
            message_type=message.message_type,
            proposal_id=message.proposal_id,
            approval_request_id=message.approval_request_id,
            text=message.text,
            message={
                "surface": message.surface,
                "recipient_id": message.recipient_id,
                "message_type": message.message_type,
                "text": message.text,
                "proposal_id": message.proposal_id,
                "approval_request_id": message.approval_request_id,
                "card": message.card,
            },
            queued_at=now,
        )
        if queued:
            store.append_event(
                "slack.message.queued",
                {
                    "dedupe_key": dedupe_key,
                    "message": message,
                    "original_surface": message.surface,
                    "delivery_surface": "slack_personal_dm",
                    "recipient_id": message.recipient_id,
                    "source": "pending_missing_info_sweep",
                },
                occurred_at=now,
            )
    return tuple(touched)


def missing_info_followup_message(
    proposal: Proposal,
    request: ApprovalRequest,
    *,
    now: datetime,
) -> OutboundMessage:
    return missing_slot_question_message(
        proposal,
        request,
        message_type="missing_info_followup",
        dedupe_key=_followup_dedupe_key(proposal, request, now=now),
    )


def _needs_missing_info_prompt(proposal: Proposal) -> bool:
    return bool(proposal.missing_slots) and proposal.status in {"draft", "posted", "awaiting_approval"}


def _pending_request_for_proposal(
    store: TeamTaskStore,
    proposal: Proposal,
    *,
    actor_id: str,
    now: datetime,
) -> tuple[ApprovalRequest, bool]:
    for request in store.list_approval_requests(
        proposal_id=proposal.proposal_id,
        approver_id=actor_id,
        status="pending",
    ):
        return request, False
    for request in store.list_approval_requests(proposal_id=proposal.proposal_id, status="pending"):
        return request, False
    approver_id = proposal.proposer_id or proposal.assigned_to or actor_id
    if approver_id not in {actor_id, proposal.assigned_to, proposal.proposer_id, *proposal.required_approvers}:
        approver_id = actor_id
    return approval_request(proposal.proposal_id, approver_id, now=now), True


def _current_initial_prompt_was_delivered(
    store: TeamTaskStore,
    proposal: Proposal,
    request: ApprovalRequest,
) -> bool:
    if request.requested_at is None or proposal.updated_at is None:
        return False
    if proposal.updated_at > request.requested_at:
        return False
    return store.has_outbound_delivery(
        f"slack-outbound/{request.approver_id}/approval_request/{request.request_id}"
    )


def _followup_dedupe_key(proposal: Proposal, request: ApprovalRequest, *, now: datetime | None = None) -> str:
    content = "|".join(
        (
            proposal.proposal_id,
            request.request_id,
            proposal.title,
            ",".join(proposal.missing_slots),
            proposal.due_date.isoformat() if proposal.due_date else "",
            proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
            proposal.time_window,
            proposal.updated_at.isoformat(timespec="seconds") if proposal.updated_at else "",
            now.isoformat(timespec="seconds") if now and not proposal.updated_at else "",
        )
    )
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    return f"slack-missing-info-followup/{request.request_id}/{digest}"

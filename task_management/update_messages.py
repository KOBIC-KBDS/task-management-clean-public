from __future__ import annotations

from .relations import (
    COMPLETED_AT_KEY,
    DEFERRED_UNTIL_KEY,
    LAST_SEMANTIC_PATCH_AT_KEY,
    LAST_SEMANTIC_PATCH_EVIDENCE_KEY,
    LAST_STATE_LINKED_UPDATE_TYPE_KEY,
    LOCATION_KEY,
    PROGRESS_NOTE_KEY,
    PROGRESS_PERCENT_KEY,
    PROGRESS_STATUS_KEY,
    PROGRESS_UPDATED_AT_KEY,
    REMAINING_WORK_KEY,
)

import hashlib

from .approval_policy import team_message as _team_message
from .domain import OutboundMessage, Proposal
from .operating_agent import ProposalPatch


def _shared_state_branch(proposal: Proposal, *, actor_id: str, suffix: str) -> OutboundMessage | None:
    """Render the completion/progress/deferral branches shared by the semantic and
    natural state-linked update messages.

    These three cases produce byte-identical OutboundMessages regardless of the
    ``semantic_`` vs ``natural_`` prefix on ``last_state_linked_update_type`` so they
    are dispatched on the prefix-stripped suffix. Returns ``None`` for any other
    suffix so callers can fall through to their prefix-specific branches.
    """

    if suffix == "completion":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_completed",
            text=f"완료로 표시했습니다: {proposal.title}",
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                COMPLETED_AT_KEY: proposal.metadata.get(COMPLETED_AT_KEY, ""),
            },
        )
    if suffix == "progress":
        remaining = proposal.metadata.get(REMAINING_WORK_KEY, "")
        suffix_text = f"\n남은 일: {remaining}" if remaining else ""
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_progress_updated",
            text=f"진행 상황을 기록했습니다: {proposal.title}{suffix_text}",
            proposal_id=proposal.proposal_id,
            card={
                "dedupe_key": _progress_update_dedupe_key(
                    proposal,
                    actor_id=actor_id,
                    message_type="proposal_progress_updated",
                ),
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                PROGRESS_STATUS_KEY: proposal.metadata.get(PROGRESS_STATUS_KEY, ""),
                PROGRESS_PERCENT_KEY: proposal.metadata.get(PROGRESS_PERCENT_KEY, ""),
                REMAINING_WORK_KEY: remaining,
            },
        )
    if suffix == "deferral":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        detail = " ".join(item for item in (when, proposal.time_window) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_deferred",
            text=f"일정을 미뤄 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                "time": proposal.time_window,
            },
        )
    return None


def _semantic_direct_update_message(proposal: Proposal, *, actor_id: str) -> OutboundMessage:
    update_type = proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY)
    if isinstance(update_type, str) and update_type.startswith("semantic_"):
        shared = _shared_state_branch(proposal, actor_id=actor_id, suffix=update_type[len("semantic_"):])
        if shared is not None:
            return shared
    if proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY) == "semantic_confirmation":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        detail = " ".join(item for item in (when, proposal.time_window) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_confirmed",
            text=f"일정 확정을 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                "time": proposal.time_window,
            },
        )
    details = []
    if proposal.scheduled_date:
        details.append(proposal.scheduled_date.isoformat())
    elif proposal.due_date:
        details.append(proposal.due_date.isoformat())
    if proposal.time_window:
        details.append(proposal.time_window)
    if proposal.metadata.get(LOCATION_KEY):
        details.append(proposal.metadata[LOCATION_KEY])
    suffix = f" ({' · '.join(details)})" if details else ""
    missing = f"\n아직 필요한 정보: {', '.join(proposal.missing_slots)}" if proposal.missing_slots else ""
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="semantic_patch_applied",
        text=f"기존 작업에 반영했습니다: {proposal.title}{suffix}{missing}",
        proposal_id=proposal.proposal_id,
        card={
            "proposal_id": proposal.proposal_id,
            "title": proposal.title,
            "status": proposal.status,
            "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
            "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
            "time": proposal.time_window,
            LOCATION_KEY: proposal.metadata.get(LOCATION_KEY, ""),
            "missing_slots": ", ".join(proposal.missing_slots),
        },
    )


def _state_update_message(proposal: Proposal, *, actor_id: str) -> OutboundMessage:
    update_type = proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY)
    if isinstance(update_type, str) and update_type.startswith("natural_"):
        shared = _shared_state_branch(proposal, actor_id=actor_id, suffix=update_type[len("natural_"):])
        if shared is not None:
            return shared
    if proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY) == "conflict_resolution_applied":
        action = proposal.metadata.get("conflict_resolution_action", "")
        existing = proposal.metadata.get("conflict_resolved_existing_titles", "")
        if action in {"not_attending_existing", "cancel_existing"}:
            detail = f"‘{existing}’은 불참/제외 처리하고, " if existing else ""
            text = f"반영했습니다. {detail}{proposal.title} 일정은 확정했습니다."
        elif action == "keep_both":
            text = f"반영했습니다. 기존 일정은 유지하고, {proposal.title} 일정도 확정했습니다."
        else:
            text = f"반영했습니다. {proposal.title} 일정 충돌 처리를 완료했습니다."
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="conflict_resolved",
            text=text,
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "conflict_resolution_action": action,
                "conflict_resolved_existing_titles": existing,
            },
        )
    if proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY) == "conflict_resolution_deferred":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="conflict_resolution_deferred",
            text=f"알겠습니다. {proposal.title} 일정 충돌은 일단 보류 상태로 두겠습니다.",
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
            },
        )
    if proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY) == "missing_info_resolved":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        location = proposal.metadata.get(LOCATION_KEY, "")
        detail = " ".join(item for item in (when, proposal.time_window, location) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_approved" if proposal.status == "approved" else "proposal_changed",
            text=f"일정 정보를 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "scheduled_date": when,
                "time": proposal.time_window,
                LOCATION_KEY: location,
                "missing_slots": ", ".join(proposal.missing_slots),
            },
        )
    if proposal.metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY) == "missing_info_deferred":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="missing_info_deferred",
            text=(
                f"알겠습니다. {proposal.title}은(는) 아직 정해지지 않은 정보가 있는 상태로 두고, "
                "필요한 시점에 다시 확인하겠습니다."
            ),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "missing_slots": ", ".join(proposal.missing_slots),
                DEFERRED_UNTIL_KEY: proposal.metadata.get(DEFERRED_UNTIL_KEY, ""),
            },
        )
    return _team_message(
        proposal,
        message_type="proposal_changed",
        text=f"일정 업데이트를 반영했습니다: {proposal.title}",
    )


def _progress_update_dedupe_key(proposal: Proposal, *, actor_id: str, message_type: str) -> str:
    token_source = "|".join(
        (
            proposal.proposal_id,
            proposal.metadata.get(PROGRESS_UPDATED_AT_KEY, ""),
            proposal.metadata.get(LAST_SEMANTIC_PATCH_AT_KEY, ""),
            proposal.metadata.get(LAST_SEMANTIC_PATCH_EVIDENCE_KEY, ""),
            proposal.metadata.get(PROGRESS_NOTE_KEY, ""),
            proposal.metadata.get(REMAINING_WORK_KEY, ""),
            proposal.time_window,
            proposal.due_date.isoformat() if proposal.due_date else "",
            proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
        )
    )
    token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:16]
    return f"slack-outbound/{actor_id}/{message_type}/{proposal.proposal_id}/{token}"


def _agent_clarification_message(
    *,
    recipient_id: str,
    prompt: str,
    proposal_id: str,
    missing_slots: tuple[str, ...],
) -> OutboundMessage:
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=recipient_id,
        message_type="agent_clarification",
        text=prompt,
        proposal_id=proposal_id,
        card={
            "proposal_id": proposal_id,
            "missing_slots": ", ".join(missing_slots),
        },
    )


def _patch_rejection_message(patch: ProposalPatch, *, actor_id: str, reason: str) -> OutboundMessage:
    if reason == "low_target_confidence":
        text = (
            "어느 작업에 반영할지 확신이 낮아 자동으로 바꾸지 않았습니다.\n"
            f"제가 이해한 근거: {patch.evidence_text or patch.body}\n"
            "어떤 작업을 말하는지 한 번 더 알려주세요."
        )
    elif reason == "agent_requested_clarification":
        slots = ", ".join(patch.missing_slots) if patch.missing_slots else "추가 정보"
        text = (
            "아직 바로 반영하기에는 정보가 부족합니다.\n"
            f"확인이 필요한 항목: *{slots}*\n"
            f"제가 이해한 근거: {patch.evidence_text or patch.body}"
        )
    else:
        text = (
            "이 답변을 안전하게 반영하지 않았습니다.\n"
            f"사유: {reason}\n"
            "대상 작업이나 승인 요청을 다시 확인해주세요."
        )
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="agent_patch_rejected",
        text=text,
        proposal_id=patch.proposal_id,
        approval_request_id=patch.request_id,
        card={
            "proposal_id": patch.proposal_id,
            "request_id": patch.request_id,
            "reason": reason,
            "target_confidence": f"{patch.target_confidence:.2f}",
            "evidence_text": patch.evidence_text,
        },
    )

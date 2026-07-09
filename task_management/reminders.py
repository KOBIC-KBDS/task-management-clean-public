from __future__ import annotations

from .relations import (
    DEFERRED_REMINDER_CADENCE_HOURS_KEY,
    DEFERRED_UNTIL_KEY,
    LINK_PREP_SUBTASK,
    LINK_TYPE_KEY,
    LOCATION_KEY,
    PARTICIPANTS_KEY,
)

from datetime import date, datetime

from .domain import KIND_SPECS, OutboundMessage, Proposal
from .human_view import (
    build_missing_slot_question,
    proposal_participants_label,
    render_missing_slot_reminder,
    time_label,
)
from .outbound_delivery import reserve_outbound
from .store import TeamTaskStore


def build_due_reminders(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    reserve: bool = True,
    pending_info_cadence_hours: int = 6,
) -> tuple[OutboundMessage, ...]:
    today = now.date()
    reminders: list[OutboundMessage] = []
    for proposal in store.list_proposals():
        if proposal.status != "approved" or not KIND_SPECS[proposal.kind].reminder_message_type:
            continue
        occurrence = _occurrence_date(proposal)
        if occurrence != today:
            continue
        dedupe_key = reminder_dedupe_key(proposal, today)
        if store.has_outbound_delivery(dedupe_key):
            continue
        prep = _outstanding_prep(store, proposal)
        text = _reminder_text(proposal, prep)
        message = OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type=KIND_SPECS[proposal.kind].reminder_message_type,
            text=text,
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "kind": proposal.kind,
                "status": proposal.status,
                "time": proposal.time_window,
                LOCATION_KEY: proposal.metadata.get(LOCATION_KEY, ""),
                PARTICIPANTS_KEY: proposal.metadata.get(PARTICIPANTS_KEY, ""),
                "outstanding_prep": ", ".join(item.title for item in prep),
            },
        )
        if reserve and not reserve_outbound(
            store,
            message,
            dedupe_key=dedupe_key,
            provider_message_id="reminder-preview",
            event_type="reminder.created",
            event_payload={"dedupe_key": dedupe_key, "proposal_id": proposal.proposal_id},
            now=now,
        ):
            continue
        reminders.append(message)
    reminders.extend(
        _build_pending_info_reminders(
            store,
            now=now,
            actor_id=actor_id,
            reserve=reserve,
            cadence_hours=pending_info_cadence_hours,
        )
    )
    return tuple(reminders)


def reminder_dedupe_key(proposal: Proposal, occurrence: date) -> str:
    return f"slack-reminder/{proposal.proposal_id}/{occurrence.isoformat()}"


def pending_info_reminder_dedupe_key(request_id: str, *, now: datetime, cadence_hours: int) -> str:
    bucket = max(0, now.hour // max(1, cadence_hours))
    return f"slack-pending-info/{request_id}/{now.date().isoformat()}/{bucket}"


def _occurrence_date(proposal: Proposal) -> date | None:
    raw = proposal.metadata.get("next_occurrence_date") or proposal.metadata.get("routine_occurrence_date")
    if raw:
        return date.fromisoformat(raw)
    return proposal.scheduled_date


def _outstanding_prep(store: TeamTaskStore, proposal: Proposal) -> tuple[Proposal, ...]:
    return tuple(
        item
        for item in store.list_proposals()
        if item.metadata.get("parent_proposal_id") == proposal.proposal_id
        and item.metadata.get(LINK_TYPE_KEY) == LINK_PREP_SUBTASK
        and item.status not in {"done", "applied", "rejected"}
    )


def _reminder_text(proposal: Proposal, prep: tuple[Proposal, ...]) -> str:
    parts = [f"*오늘 일정 리마인드*\n{proposal.title} 일정이 오늘 있습니다."]
    if proposal.time_window:
        parts.append(f"시간: {time_label(proposal.time_window)}")
    if proposal.metadata.get(LOCATION_KEY):
        parts.append(f"장소: {proposal.metadata[LOCATION_KEY]}")
    participants = proposal_participants_label(proposal)
    if participants:
        parts.append(f"참여자: {participants}")
    if prep:
        parts.append("남은 준비: " + ", ".join(item.title for item in prep))
    return "\n".join(parts)


def _build_pending_info_reminders(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str,
    reserve: bool,
    cadence_hours: int,
) -> tuple[OutboundMessage, ...]:
    messages: list[OutboundMessage] = []
    for request in store.list_approval_requests(approver_id=actor_id, status="pending"):
        proposal = store.get_proposal(request.proposal_id)
        if proposal is None or not proposal.missing_slots:
            continue
        if not _deferred_slot_is_due(proposal, now=now):
            continue
        proposal_cadence = _proposal_pending_info_cadence(proposal, default=cadence_hours)
        dedupe_key = pending_info_reminder_dedupe_key(
            request.request_id,
            now=now,
            cadence_hours=proposal_cadence,
        )
        if store.has_outbound_delivery(dedupe_key):
            continue
        text = _pending_info_text(proposal, request.request_id)
        message = OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="missing_info_reminder",
            text=text,
            proposal_id=proposal.proposal_id,
            approval_request_id=request.request_id,
            card={
                "proposal_id": proposal.proposal_id,
                "request_id": request.request_id,
                "title": proposal.title,
                "missing_slots": ", ".join(proposal.missing_slots),
            },
        )
        if reserve and not reserve_outbound(
            store,
            message,
            dedupe_key=dedupe_key,
            provider_message_id="pending-info-reminder-preview",
            event_type="reminder.created",
            event_payload={
                "dedupe_key": dedupe_key,
                "proposal_id": proposal.proposal_id,
                "request_id": request.request_id,
                "kind": "missing_info",
            },
            now=now,
        ):
            continue
        messages.append(message)
    return tuple(messages)


def _pending_info_text(proposal: Proposal, request_id: str) -> str:
    return render_missing_slot_reminder(build_missing_slot_question(proposal, request_id=request_id))


def _deferred_slot_is_due(proposal: Proposal, *, now: datetime) -> bool:
    raw = proposal.metadata.get(DEFERRED_UNTIL_KEY, "")
    if not raw:
        return True
    try:
        return now >= datetime.fromisoformat(raw)
    except ValueError:
        # Conservative: an unparseable deferred_until is not treated as due, so a
        # malformed value does not fire an immediate pending-info reminder. It
        # stays quiet until corrected to a parseable value.
        return False


def _proposal_pending_info_cadence(proposal: Proposal, *, default: int) -> int:
    raw = proposal.metadata.get(DEFERRED_REMINDER_CADENCE_HOURS_KEY, "")
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default

from __future__ import annotations

from .relations import (
    CONFLICT_DETECTED_KEY,
    CONFLICT_WITH_PROPOSAL_IDS_KEY,
    DATE_WINDOW_END_KEY,
    DATE_WINDOW_START_KEY,
    LAST_STATE_LINKED_UPDATE_TYPE_KEY,
    LINK_PREP_SUBTASK,
    LINK_TYPE_KEY,
    LOCATION_KEY,
    PARTICIPANTS_KEY,
)

import re
from dataclasses import replace
from datetime import date, datetime

from .approval_policy import approval_request
from .domain import KIND_SPECS, ApprovalDecision, ApprovalRequest, IncomingMessage, OutboundMessage, Proposal
from .human_view import date_label, date_range_label
from .slot_validator import missing_slots_for_proposal
from .sort_keys import NO_TIME_MINUTES, time_sort_minutes
from .store import TeamTaskStore


CONFLICT_SLOT = "conflict_resolution"

# When only a start time is known (no end/duration), treat the event as a short
# point-window so two exact-time same-day events still collide if they start
# within this many minutes of each other. Conservative on purpose: a known start
# with an unknown duration should not silently clear a real overlap.
_POINT_WINDOW_MINUTES = 60

ConflictAction = str


def conflict_resolution_recorded(proposal: Proposal) -> bool:
    """True once a conflict decision has been applied to ``proposal``.

    Both the parent-approval path (:func:`_approve_conflict_parent`) and the
    not-attending path stamp ``conflict_resolution_action`` into metadata, so its
    presence is the canonical signal that the conflict slot may clear.
    """

    metadata = proposal.metadata
    return bool(
        metadata.get("conflict_resolution_action")
        or metadata.get(LAST_STATE_LINKED_UPDATE_TYPE_KEY)
        in {"conflict_resolution_applied", "conflict_resolution_deferred"}
    )


def recompute_missing_slots(proposal: Proposal) -> tuple[str, ...]:
    """Recompute machine slots, preserving a still-open conflict hold.

    :func:`missing_slots_for_proposal` has no notion of the conflict-resolution
    slot, so a held-for-conflict proposal would silently lose it on any
    date/feedback/semantic recompute. This additive guard re-adds
    :data:`CONFLICT_SLOT` whenever the proposal is still flagged
    ``conflict_detected`` and no resolution has been recorded yet.
    """

    missing = missing_slots_for_proposal(proposal)
    if proposal.metadata.get(CONFLICT_DETECTED_KEY) == "true" and not conflict_resolution_recorded(proposal):
        if CONFLICT_SLOT not in missing:
            missing = (*missing, CONFLICT_SLOT)
    return missing


def apply_conflict_policy(
    store: TeamTaskStore,
    proposal: Proposal,
    *,
    actor_id: str,
    now: datetime,
) -> tuple[Proposal, tuple[ApprovalRequest, ...], tuple[OutboundMessage, ...]]:
    """Hold new blocking events when they overlap existing approved events.

    The operating agent may identify a large schedule block such as a 출장/training
    period.  The deterministic core owns the safety rule: if that new block
    overlaps an already-approved in-person event for the same actor, do not
    silently approve or cancel either item. Ask the actor for a conflict decision.
    """

    conflicts = _blocking_conflicts(store, proposal)
    if not conflicts:
        return proposal, (), ()

    conflict_ids = ",".join(item.proposal_id for item in conflicts)
    missing_slots = tuple(dict.fromkeys((*proposal.missing_slots, CONFLICT_SLOT)))
    held = replace(
        proposal,
        kind="question",
        status="awaiting_approval",
        required_approvers=(actor_id,),
        approvals=(),
        missing_slots=missing_slots,
        metadata={
            **proposal.metadata,
            CONFLICT_DETECTED_KEY: "true",
            "conflict_detected_at": now.isoformat(timespec="seconds"),
            CONFLICT_WITH_PROPOSAL_IDS_KEY: conflict_ids,
            "conflict_policy": "ask_before_mutating_existing_events",
        },
        updated_at=now,
    )
    request = approval_request(held.proposal_id, actor_id, now=now)
    return held, (request,), (_conflict_message(held, conflicts, request),)


PREP_SUBTASK_LINK_TYPE = LINK_PREP_SUBTASK
_PREP_TERMINAL_STATUSES = {"done", "rejected"}


def cancel_prep_subtasks(
    store: TeamTaskStore,
    parent_proposal_id: str,
    *,
    now: datetime,
    reason: str,
) -> tuple[Proposal, ...]:
    """Reject auto-created '자료 준비' children when their parent is rejected/cancelled.

    Sweeps the store for ``prep_subtask`` children linked to ``parent_proposal_id``
    that are still live (status not done/rejected) and rejects each so it stops
    surfacing in briefings/EOD. Lives here (not in the orchestrator) so both the
    orchestrator reject paths and the conflict not-attending path can call it
    without an import cycle. Returns the children it rejected.

    INVARIANT 4 is preserved: a rejected child carries no approvals/required
    approvers, so ``status=approved IFF required_approvers ⊆ approvals`` cannot be
    falsely satisfied.
    """

    rejected: list[Proposal] = []
    for child in store.list_proposals():
        metadata = child.metadata
        if metadata.get("parent_proposal_id") != parent_proposal_id:
            continue
        if metadata.get(LINK_TYPE_KEY) != PREP_SUBTASK_LINK_TYPE:
            continue
        if child.status in _PREP_TERMINAL_STATUSES:
            continue
        updated = replace(
            child,
            status="rejected",
            approvals=(),
            required_approvers=(),
            missing_slots=(),
            metadata={
                **metadata,
                "previous_status": child.status,
                "cascade_rejected_reason": reason,
                "cascade_rejected_parent_proposal_id": parent_proposal_id,
                "cascade_rejected_at": now.isoformat(timespec="seconds"),
            },
            updated_at=now,
        )
        store.save_proposal(updated)
        store.append_event(
            "proposal.rejected",
            {
                "proposal": updated,
                "reason": reason,
                "parent_proposal_id": parent_proposal_id,
                "cascade": True,
            },
            occurred_at=now,
        )
        rejected.append(updated)
    return tuple(rejected)


def apply_conflict_resolution_feedback(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> Proposal | None:
    """Apply a natural-language answer to a pending schedule conflict.

    Supported MVP answers are intentionally small and conservative:
    - existing event cannot be attended / should be canceled
    - both schedules can stay
    - decision is deferred
    """

    action = parse_conflict_action(message.text)
    if action == "":
        return None
    proposal = _pending_conflict_for_actor(store, actor_id=message.sender_id)
    if proposal is None:
        return None

    if action == "defer":
        updated = replace(
            proposal,
            metadata={
                **proposal.metadata,
                LAST_STATE_LINKED_UPDATE_TYPE_KEY: "conflict_resolution_deferred",
                "conflict_resolution_answer": message.text,
                "conflict_resolution_answer_message_id": message.message_id,
                "conflict_resolution_answered_at": now.isoformat(timespec="seconds"),
            },
            updated_at=now,
        )
        store.save_proposal(updated)
        store.append_event(
            "proposal.changed",
            {"proposal": updated, "change_type": "conflict_resolution_deferred", "actor_id": message.sender_id},
            occurred_at=now,
        )
        return updated

    conflict_ids = _conflict_ids(proposal)
    conflicts = tuple(item for item in (store.get_proposal(pid) for pid in conflict_ids) if item is not None)
    if action in {"not_attending_existing", "cancel_existing"}:
        for conflict in conflicts:
            _mark_existing_conflict_not_attending(
                store,
                conflict,
                parent=proposal,
                actor_id=message.sender_id,
                answer=message.text,
                now=now,
            )

    approved = _approve_conflict_parent(
        store,
        proposal,
        action=action,
        actor_id=message.sender_id,
        answer=message.text,
        conflicts=conflicts,
        now=now,
    )
    return approved


def parse_conflict_action(text: str) -> ConflictAction:
    normalized = text.replace(" ", "").lower()
    if any(token in normalized for token in ("보류", "나중", "아직", "미정")):
        return "defer"
    if any(token in normalized for token in ("둘다", "둘다가능", "참석가능", "갈수", "갈수있", "가능")) and not any(
        token in normalized for token in ("못", "불참", "취소")
    ):
        return "keep_both"
    if "취소" in normalized:
        return "cancel_existing"
    if any(token in normalized for token in ("불참", "참석하지못", "못할", "못갈", "못가", "못함")):
        return "not_attending_existing"
    return ""


def _blocking_conflicts(store: TeamTaskStore, proposal: Proposal) -> tuple[Proposal, ...]:
    if not _is_blocking_event(proposal):
        return ()
    start, end = _proposal_date_range(proposal)
    if start is None:
        return ()
    conflicts: list[Proposal] = []
    for existing in store.list_proposals():
        if existing.proposal_id == proposal.proposal_id:
            continue
        if existing.status not in {"approved", "applied"}:
            continue
        if not KIND_SPECS[existing.kind].conflict_participant:
            continue
        if not _same_actor(proposal, existing):
            continue
        existing_start, existing_end = _proposal_date_range(existing)
        if existing_start is None:
            continue
        if not _ranges_overlap(start, end, existing_start, existing_end):
            continue
        if not _times_overlap(proposal, existing):
            continue
        conflicts.append(existing)
    return tuple(sorted(conflicts, key=lambda item: (_date_label(item), item.time_window, item.title)))


def _pending_conflict_for_actor(store: TeamTaskStore, *, actor_id: str) -> Proposal | None:
    pending_requests = store.list_approval_requests(approver_id=actor_id, status="pending")
    candidates: list[Proposal] = []
    for request in pending_requests:
        proposal = store.get_proposal(request.proposal_id)
        if proposal is None:
            continue
        if proposal.status != "awaiting_approval":
            continue
        if proposal.metadata.get(CONFLICT_DETECTED_KEY) == "true" and CONFLICT_SLOT in proposal.missing_slots:
            candidates.append(proposal)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.updated_at or item.created_at or datetime.min)[-1]


def _conflict_ids(proposal: Proposal) -> tuple[str, ...]:
    raw = proposal.metadata.get(CONFLICT_WITH_PROPOSAL_IDS_KEY, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _mark_existing_conflict_not_attending(
    store: TeamTaskStore,
    conflict: Proposal,
    *,
    parent: Proposal,
    actor_id: str,
    answer: str,
    now: datetime,
) -> Proposal:
    updated = replace(
        conflict,
        status="rejected",
        metadata={
            **conflict.metadata,
            "attendance_status": "not_attending",
            "previous_status": conflict.status,
            "conflict_resolution_action": "not_attending_existing",
            "conflict_resolution_parent_proposal_id": parent.proposal_id,
            "conflict_resolution_actor_id": actor_id,
            "conflict_resolution_answer": answer,
            "conflict_resolution_at": now.isoformat(timespec="seconds"),
        },
        updated_at=now,
    )
    store.save_proposal(updated)
    store.append_event(
        "proposal.changed",
        {
            "proposal": updated,
            "change_type": "conflict_existing_not_attending",
            "actor_id": actor_id,
            "parent_proposal_id": parent.proposal_id,
        },
        occurred_at=now,
    )
    store.append_event(
        "proposal.rejected",
        {
            "proposal": updated,
            "reason": "conflict_resolution_not_attending",
            "parent_proposal_id": parent.proposal_id,
        },
        occurred_at=now,
    )
    cancel_prep_subtasks(
        store,
        conflict.proposal_id,
        now=now,
        reason="parent_conflict_not_attending",
    )
    return updated


def _approve_conflict_parent(
    store: TeamTaskStore,
    proposal: Proposal,
    *,
    action: ConflictAction,
    actor_id: str,
    answer: str,
    conflicts: tuple[Proposal, ...],
    now: datetime,
) -> Proposal:
    request = _pending_request_for_proposal(store, proposal.proposal_id, actor_id=actor_id)
    if request is not None:
        decided = replace(request, status="accepted", decided_at=now)
        decision = ApprovalDecision(
            request_id=request.request_id,
            proposal_id=request.proposal_id,
            approver_id=actor_id,
            decision="accepted",
            decided_at=now,
        )
        store.save_approval_request(decided)
        store.save_approval_decision(decision)
        store.append_event(
            "approval.accepted",
            {"decision": decision, "reconciled": True, "reason": "conflict_resolution"},
            occurred_at=now,
        )

    missing_slots = tuple(item for item in proposal.missing_slots if item != CONFLICT_SLOT)
    approvals = tuple(sorted(set((*proposal.approvals, actor_id))))
    required = proposal.required_approvers or (actor_id,)
    status = "approved" if not missing_slots and set(required).issubset(approvals) else "awaiting_approval"
    kind = "event" if proposal.scheduled_date is not None else proposal.kind
    updated = replace(
        proposal,
        kind=kind,
        status=status,
        approvals=approvals,
        required_approvers=required,
        missing_slots=missing_slots,
        metadata={
            **proposal.metadata,
            LAST_STATE_LINKED_UPDATE_TYPE_KEY: "conflict_resolution_applied",
            "conflict_resolution_action": action,
            "conflict_resolution_answer": answer,
            "conflict_resolution_actor_id": actor_id,
            "conflict_resolution_at": now.isoformat(timespec="seconds"),
            "conflict_resolved_existing_titles": " / ".join(item.title for item in conflicts),
        },
        updated_at=now,
    )
    store.save_proposal(updated)
    store.append_event(
        "proposal.changed",
        {"proposal": updated, "change_type": "conflict_resolution_applied", "actor_id": actor_id},
        occurred_at=now,
    )
    if updated.status == "approved":
        store.append_event(
            "proposal.approved",
            {"proposal": updated, "reason": "conflict_resolution"},
            occurred_at=now,
        )
    return updated


def _pending_request_for_proposal(store: TeamTaskStore, proposal_id: str, *, actor_id: str) -> ApprovalRequest | None:
    requests = store.list_approval_requests(proposal_id=proposal_id, approver_id=actor_id, status="pending")
    if requests:
        return requests[0]
    requests = store.list_approval_requests(proposal_id=proposal_id, status="pending")
    return requests[0] if requests else None


def _is_blocking_event(proposal: Proposal) -> bool:
    if not KIND_SPECS[proposal.kind].conflict_participant:
        return False
    metadata = proposal.metadata
    if metadata.get("blocks_in_person") == "true" or metadata.get("event_scope") in {"away", "travel", "출장"}:
        return True
    text = f"{proposal.title} {proposal.raw_text}"
    return "출장" in text or "입과" in text or "교육" in text


def _proposal_date_range(proposal: Proposal) -> tuple[date | None, date | None]:
    start = _date_from_text(proposal.metadata.get(DATE_WINDOW_START_KEY, ""))
    end = _date_from_text(proposal.metadata.get(DATE_WINDOW_END_KEY, ""))
    if start is None:
        start = proposal.scheduled_date or proposal.due_date
    if end is None:
        end = start
    return start, end


def _date_from_text(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _same_actor(left: Proposal, right: Proposal) -> bool:
    left_actors = _actor_set(left)
    right_actors = _actor_set(right)
    if "shared" in left_actors or "shared" in right_actors:
        return bool(left_actors.intersection(right_actors | {"me", "teammate", "shared"}))
    return bool(left_actors.intersection(right_actors))


def _actor_set(proposal: Proposal) -> set[str]:
    actors = {proposal.assigned_to, proposal.proposer_id}
    participants = proposal.metadata.get(PARTICIPANTS_KEY, "")
    actors.update(item.strip() for item in participants.split(",") if item.strip())
    return {item for item in actors if item and item != "unassigned"}


def _ranges_overlap(left_start: date, left_end: date | None, right_start: date, right_end: date | None) -> bool:
    return left_start <= (right_end or right_start) and right_start <= (left_end or left_start)


def _is_single_day(proposal: Proposal) -> bool:
    """True when the proposal occupies exactly one calendar day.

    Multi-day blocks declare a ``date_window_start``/``date_window_end`` span; only
    such an explicit multi-day window disqualifies an event from the finer
    time-interval comparison. A single recorded date (or matching start/end) is
    treated as single-day.
    """

    start, end = _proposal_date_range(proposal)
    if start is None:
        return False
    return end is None or end == start


# Exact clock tokens, mirroring the patterns sort_keys.time_sort_minutes treats
# as precise: Korean "N시 [N분]", "HH:MM", or the explicit 자정 (midnight). Broad
# period tokens (오전/오후/morning/afternoon/저녁/...) are deliberately excluded so
# a period-only side stays on the conservative date-level comparison.
_EXACT_CLOCK_RE = re.compile(r"(?:\d{1,2}\s*시)|(?:\b\d{1,2}:\d{2}\b)|자정")


def _has_exact_clock(value: str) -> bool:
    return _EXACT_CLOCK_RE.search(value.strip().lower()) is not None


def _exact_time_window(time_window: str) -> tuple[int, int] | None:
    """Return ``(start_min, end_min)`` for an exact clock time, else ``None``.

    Only genuine clock times resolve to a window. Period-only tokens (오전/오후/
    morning/afternoon/없음) and unknown/empty times return ``None`` so the caller
    falls back to the conservative date-only comparison. A start time without a
    paired end is widened to a short point window (:data:`_POINT_WINDOW_MINUTES`).

    Minute resolution is reused from :func:`sort_keys.time_sort_minutes`; the
    exact-clock gate (:func:`_has_exact_clock`) keeps period-only tokens out even
    though ``time_sort_minutes`` would assign them a nominal minute.
    """

    text = time_window.strip()
    if not text:
        return None
    separator = next((sep for sep in ("~", "-", "–", "—") if sep in text), None)
    if separator is not None:
        left, _, right = text.partition(separator)
        if not _has_exact_clock(left):
            return None
        start = time_sort_minutes(left)
        if start == NO_TIME_MINUTES:
            return None
        if _has_exact_clock(right):
            end = time_sort_minutes(right)
            if end == NO_TIME_MINUTES or end <= start:
                end = start + _POINT_WINDOW_MINUTES
        else:
            end = start + _POINT_WINDOW_MINUTES
        return start, end
    if not _has_exact_clock(text):
        return None
    start = time_sort_minutes(text)
    if start == NO_TIME_MINUTES:
        return None
    return start, start + _POINT_WINDOW_MINUTES


def _times_overlap(left: Proposal, right: Proposal) -> bool:
    """Decide whether two same-actor, date-overlapping events truly collide.

    Only when BOTH sides are single-day AND both expose a parseable exact clock
    time do we require an actual time-interval intersection. If either side is
    multi-day or carries a period-only/unknown time, the date-level overlap stands
    (conservative) and this returns ``True``.
    """

    if not (_is_single_day(left) and _is_single_day(right)):
        return True
    left_window = _exact_time_window(left.time_window)
    right_window = _exact_time_window(right.time_window)
    if left_window is None or right_window is None:
        return True
    left_start, left_end = left_window
    right_start, right_end = right_window
    return left_start < right_end and right_start < left_end


def _conflict_message(proposal: Proposal, conflicts: tuple[Proposal, ...], request: ApprovalRequest) -> OutboundMessage:
    conflict_lines = "\n".join(f"• {_human_event(item)}" for item in conflicts)
    new_range = _date_label(proposal)
    text = (
        "_일정 충돌 확인이 필요합니다._\n"
        f"새로 들어온 *{proposal.title}* 일정은 *{new_range}* 동안 기존 일정과 겹칩니다.\n\n"
        f"*겹치는 확정 일정*\n{conflict_lines}\n\n"
        "어떻게 처리할지 알려주세요.\n"
        "예: `점심회식 불참 처리`, `출장 중에도 참석 가능`, `기존 일정 취소`, `일단 보류`"
    )
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=request.approver_id,
        message_type="schedule_conflict",
        text=text,
        proposal_id=proposal.proposal_id,
        approval_request_id=request.request_id,
        card={
            "proposal_id": proposal.proposal_id,
            "request_id": request.request_id,
            "conflict_with": ",".join(item.proposal_id for item in conflicts),
            "missing_slots": ",".join(proposal.missing_slots),
        },
    )


def _human_event(proposal: Proposal) -> str:
    parts = [f"*{proposal.title}*"]
    when = _date_label(proposal)
    if when:
        parts.append(when)
    if proposal.time_window:
        parts.append(proposal.time_window)
    location = proposal.metadata.get(LOCATION_KEY, "")
    if location:
        parts.append(location)
    return " · ".join(parts)


def _date_label(proposal: Proposal) -> str:
    start, end = _proposal_date_range(proposal)
    if start is None:
        return ""
    if end is not None and end != start:
        return date_range_label(start, end)
    return date_label(start)

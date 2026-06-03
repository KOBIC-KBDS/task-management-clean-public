from __future__ import annotations

from datetime import date

from .domain import Proposal


OPEN_STATUSES = frozenset({"draft", "posted", "awaiting_approval", "approved", "applied"})
PROGRESS_CONFIRMATION_STATUSES = frozenset({"approved", "applied"})


def is_open_work_item(proposal: Proposal) -> bool:
    return proposal.status in OPEN_STATUSES


def is_due_overdue(proposal: Proposal, *, today: date) -> bool:
    return is_open_work_item(proposal) and proposal.due_date is not None and proposal.due_date < today


def is_scheduled_commitment(proposal: Proposal) -> bool:
    if proposal.kind != "event" or proposal.scheduled_date is None:
        return False
    metadata = proposal.metadata
    if metadata.get("action_required") == "false" or metadata.get("resolution_required") == "false":
        return False
    if metadata.get("commitment_type") in {"reference", "FYI", "fyi"}:
        return False
    return True


def is_past_scheduled_commitment(proposal: Proposal, *, today: date) -> bool:
    return is_open_work_item(proposal) and is_scheduled_commitment(proposal) and proposal.scheduled_date < today


def needs_time_resolution(proposal: Proposal, *, today: date) -> bool:
    return is_due_overdue(proposal, today=today) or is_past_scheduled_commitment(proposal, today=today)


def requires_progress_confirmation(proposal: Proposal, *, today: date) -> bool:
    if proposal.status not in PROGRESS_CONFIRMATION_STATUSES:
        return False
    if proposal.due_date is not None and proposal.due_date <= today:
        return True
    return is_past_scheduled_commitment(proposal, today=today)


def work_item_urgency_label(proposal: Proposal, *, today: date) -> str:
    if is_due_overdue(proposal, today=today):
        return "마감 지남"
    if is_past_scheduled_commitment(proposal, today=today):
        return "일정 지남 · 결과 확인 필요"
    return ""


def work_item_due_detail_label(proposal: Proposal, *, today: date) -> str:
    if proposal.due_date is not None:
        return "기한"
    if is_past_scheduled_commitment(proposal, today=today) or proposal.scheduled_date is not None:
        return "일정"
    return "기한"

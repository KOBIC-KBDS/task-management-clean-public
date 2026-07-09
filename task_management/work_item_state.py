from __future__ import annotations

from datetime import date

from .domain import KIND_SPECS, Proposal


OPEN_STATUSES = frozenset({"draft", "posted", "awaiting_approval", "approved", "applied"})
PROGRESS_CONFIRMATION_STATUSES = frozenset({"approved", "applied"})
SURFACE_EXCLUDED_STATUSES = frozenset({"rejected"})

# Mirrors ``relations.PARTICIPANTS_KEY``. Inlined to keep this module's import
# footprint at ``domain`` only (importing ``relations`` would form a cycle via
# ``relations -> sort_keys -> work_item_state``).
_PARTICIPANTS_KEY = "participants"


def is_open_work_item(proposal: Proposal) -> bool:
    return proposal.status in OPEN_STATUSES


def is_surface_visible_item(proposal: Proposal) -> bool:
    """Return True when a proposal should appear on current-work surfaces.

    Rejected proposals remain in audit/status/preview counts, but they are not
    active schedule/work items and should not be shown in Home, briefings,
    monthly pages, dashboard work sections, or hierarchy child rollups.
    """

    return proposal.status not in SURFACE_EXCLUDED_STATUSES


def schedule_first_date(proposal: Proposal) -> date | None:
    """Schedule-first date selection used by the personal surfaces.

    Note: the frontend / collaboration views deliberately use the opposite
    due-first order, so they keep their own helpers.
    """

    return proposal.scheduled_date or proposal.due_date


def is_personal_scope(proposal: Proposal, actor_id: str) -> bool:
    participants = {
        item.strip()
        for item in proposal.metadata.get(_PARTICIPANTS_KEY, "").split(",")
        if item.strip()
    }
    return (
        actor_id
        in {
            proposal.assigned_to,
            proposal.proposer_id,
            *proposal.required_approvers,
            *proposal.approvals,
            *participants,
        }
        or proposal.assigned_to in {"shared", "unassigned"}
    )


def is_due_overdue(proposal: Proposal, *, today: date) -> bool:
    return is_open_work_item(proposal) and proposal.due_date is not None and proposal.due_date < today


def is_scheduled_commitment(proposal: Proposal) -> bool:
    if not KIND_SPECS[proposal.kind].schedulable or proposal.scheduled_date is None:
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

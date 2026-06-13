from __future__ import annotations

from datetime import date

from .domain import Proposal
from .korean_time import END_OF_DAY_MINUTES, NO_TIME_MINUTES, time_sort_minutes
from .work_item_state import needs_time_resolution, schedule_first_date


__all__ = [
    "NO_TIME_MINUTES",
    "END_OF_DAY_MINUTES",
    "time_sort_minutes",
    "proposal_deadline_sort_key",
    "schedule_first_sort_key",
]


def schedule_first_sort_key(proposal: Proposal) -> tuple[str, int, str]:
    """Schedule-first ordering for the personal secretary/page/digest surfaces.

    Same date selection as the legacy ``_sort_key`` copies (scheduled_date or
    due_date), but the same-day tie-break uses real clock minutes via
    ``time_sort_minutes`` instead of the raw ``time_window`` string. This brings
    these surfaces in line with every other deadline sort (e.g. '오전 9시' now
    sorts before '오후 2시' on the same date).
    """

    proposal_date = schedule_first_date(proposal)
    return (
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        time_sort_minutes(proposal.time_window),
        proposal.title,
    )


def proposal_deadline_sort_key(
    proposal: Proposal,
    *,
    today: date | None = None,
    overdue_last: bool = False,
) -> tuple[int, str, int, str]:
    proposal_date = proposal.due_date or proposal.scheduled_date
    overdue_bucket = 0
    if overdue_last and today is not None and needs_time_resolution(proposal, today=today):
        overdue_bucket = 1
    return (
        overdue_bucket,
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        time_sort_minutes(proposal.time_window),
        proposal.title,
    )


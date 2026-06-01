from __future__ import annotations

from datetime import date
import re

from .domain import Proposal


NO_TIME_MINUTES = 24 * 60 + 1
END_OF_DAY_MINUTES = 24 * 60


def time_sort_minutes(value: str) -> int:
    text = value.strip().lower()
    if not text:
        return NO_TIME_MINUTES
    if "자정" in text:
        return END_OF_DAY_MINUTES
    if "퇴근" in text:
        return 18 * 60

    clock = re.search(r"(?:(오전|오후|저녁|밤)\s*)?(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text)
    if clock:
        marker = clock.group(1) or ""
        hour = int(clock.group(2))
        minute = int(clock.group(3) or "0")
        if marker in {"오후", "저녁", "밤"} and hour < 12:
            hour += 12
        if marker == "오전" and hour == 12:
            hour = 0
        return hour * 60 + minute

    hhmm = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if hhmm:
        return int(hhmm.group(1)) * 60 + int(hhmm.group(2))

    broad_times = (
        ("all_day", 0),
        ("morning", 8 * 60),
        ("오전", 8 * 60),
        ("lunch", 12 * 60),
        ("noon", 12 * 60),
        ("점심", 12 * 60),
        ("afternoon", 13 * 60),
        ("오후", 13 * 60),
        ("evening", 18 * 60),
        ("저녁", 18 * 60),
        ("밤", 20 * 60),
    )
    for token, minutes in broad_times:
        if token in text:
            return minutes
    return NO_TIME_MINUTES


def proposal_deadline_sort_key(
    proposal: Proposal,
    *,
    today: date | None = None,
    overdue_last: bool = False,
) -> tuple[int, str, int, str]:
    proposal_date = proposal.due_date or proposal.scheduled_date
    overdue_bucket = 0
    if overdue_last and today is not None and _is_overdue(proposal, today=today):
        overdue_bucket = 1
    return (
        overdue_bucket,
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        time_sort_minutes(proposal.time_window),
        proposal.title,
    )


def _is_overdue(proposal: Proposal, *, today: date) -> bool:
    return (
        proposal.status not in {"done", "rejected"}
        and proposal.due_date is not None
        and proposal.due_date < today
    )

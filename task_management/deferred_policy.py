from __future__ import annotations

from datetime import date, datetime, timedelta


def csv_dedupe(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def default_deferred_until(target_date: date | None, *, changed_at: datetime) -> str:
    if target_date is None:
        return (changed_at + timedelta(hours=6)).isoformat(timespec="seconds")
    if target_date > changed_at.date():
        # Land the re-prompt before the ~08:00 morning briefing window so a
        # future-dated deferral surfaces in that day's briefing instead of being
        # hidden until after the once-a-day briefing has already fired (and its
        # dedupe key has locked the day out). 07:30 stays in the future relative
        # to prior days' briefings, so it never fires prematurely.
        return (
            datetime.combine(target_date, datetime.min.time())
            .replace(hour=7, minute=30)
            .isoformat(timespec="seconds")
        )
    return (changed_at + timedelta(hours=2)).isoformat(timespec="seconds")

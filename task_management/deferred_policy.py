from __future__ import annotations

from datetime import date, datetime, timedelta


def csv_dedupe(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def default_deferred_until(target_date: date | None, *, changed_at: datetime) -> str:
    if target_date is None:
        return (changed_at + timedelta(hours=6)).isoformat(timespec="seconds")
    if target_date > changed_at.date():
        return (
            datetime.combine(target_date, datetime.min.time())
            .replace(hour=8, minute=30)
            .isoformat(timespec="seconds")
        )
    return (changed_at + timedelta(hours=2)).isoformat(timespec="seconds")

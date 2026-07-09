"""Single home for Korean time/date vocabulary.

This module owns the one AM/PM clock regex, the broad time-of-day token table,
and the relative-date vocabulary (오늘/내일/어제) that were previously copied
across sort_keys, discussion_adapter, and task_reconciler.

Two directions are kept as separate functions over the shared tables:

* parse direction (text -> a window token / "HH:MM" string), used when ingesting
  natural-language messages, and
* sort direction (a window token -> minutes-since-midnight), used when ordering
  proposals for display.

The module deliberately imports nothing else from ``task_management`` so it can
be the shared leaf both ``sort_keys`` and ``relations`` depend on without
creating an import cycle.
"""

from __future__ import annotations

from datetime import date, timedelta
import re


# Minutes returned by the sort direction for items with no usable time.
NO_TIME_MINUTES = 24 * 60 + 1
END_OF_DAY_MINUTES = 24 * 60

# The single AM/PM clock regex.  Markers that imply afternoon/evening push a
# bare hour past noon; 오전 12시 is midnight.  Supports the "반" half-hour token.
CLOCK_RE = re.compile(
    r"(?:(오전|오후|저녁|밤)\s*)?(\d{1,2})\s*시(?:\s*(?:(\d{1,2})\s*분|반))?"
)
PM_MARKERS = {"오후", "저녁", "밤"}

# Broad time-of-day tokens shared by the sort direction.  Order matters: the
# first token found in the text wins, mirroring the historical sort_keys table.
BROAD_TIME_MINUTES = (
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


def parse_clock(text: str) -> tuple[int, int] | None:
    """Parse a Korean/AM-PM clock string into ``(hour, minute)``.

    Returns ``None`` when no ``N시`` clock token is present.  Normalization:
    a 오후/저녁/밤 marker with ``hour < 12`` adds 12; 오전 12시 becomes 0; the
    "반" token means the half hour when no explicit minute is given.
    """

    match = CLOCK_RE.search(text)
    if not match:
        return None
    hour = int(match.group(2))
    if match.group(3) is None and "반" in match.group(0):
        minute = 30
    else:
        minute = int(match.group(3) or 0)
    marker = match.group(1) or ""
    if marker in PM_MARKERS and hour < 12:
        hour += 12
    if marker == "오전" and hour == 12:
        hour = 0
    return hour, minute


def time_sort_minutes(value: str) -> int:
    """Sort direction: map a time-window string to minutes-since-midnight."""

    text = value.strip().lower()
    if not text:
        return NO_TIME_MINUTES
    if "자정" in text:
        return END_OF_DAY_MINUTES
    if "퇴근" in text:
        return 18 * 60

    parsed = parse_clock(text)
    if parsed is not None:
        hour, minute = parsed
        return hour * 60 + minute

    hhmm = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if hhmm:
        return int(hhmm.group(1)) * 60 + int(hhmm.group(2))

    for token, minutes in BROAD_TIME_MINUTES:
        if token in text:
            return minutes
    return NO_TIME_MINUTES


def relative_date(text: str, reference_date: date) -> date:
    """Resolve 내일/어제 relative to ``reference_date``.

    Mirrors the historical task_reconciler ``_target_date`` contract: 내일 is the
    next day, 어제 is the previous day, and anything else (including 오늘) falls
    back to ``reference_date`` itself.
    """

    if "내일" in text:
        return reference_date + timedelta(days=1)
    if "어제" in text:
        return reference_date - timedelta(days=1)
    return reference_date

from __future__ import annotations

from .relations import (
    ATTENDEES_KEY,
    EXTERNAL_PARTICIPANTS_KEY,
    LOCATION_KEY,
    LOCATION_OPTIONAL_KEY,
    NEEDS_EXACT_TIME_KEY,
    PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
)

import re

from .domain import TeamTaskTaskCandidate, Proposal


def missing_slots_for_candidate(candidate: TeamTaskTaskCandidate, *, assigned_to: str) -> tuple[str, ...]:
    """Return machine slot names required before a candidate can be approved."""

    missing: list[str] = []
    if assigned_to == "unassigned":
        missing.append("assigned_to")
    if candidate.metadata.get("needs_exact_date") == "true" and candidate.due_date is None and candidate.scheduled_date is None:
        missing.append("exact_date")
    if candidate.item_type == "routine":
        for key, slot in (
            ("recurrence_frequency", "recurrence"),
            ("time_window", "time"),
            (LOCATION_KEY, LOCATION_KEY),
            (PARTICIPANTS_KEY, PARTICIPANTS_KEY),
        ):
            value = candidate.metadata.get(key) if key != "time_window" else candidate.time_window
            if not value or (key == "time_window" and _is_unknown_time(value)):
                missing.append(slot)
        return _dedupe(missing)
    if candidate.item_type == "event":
        if not has_participant_metadata(candidate.metadata):
            missing.append(PARTICIPANTS_KEY)
        if _requires_exact_time(candidate.metadata, candidate.time_window):
            missing.append("time")
        if not candidate.metadata.get(LOCATION_KEY) and candidate.metadata.get(LOCATION_OPTIONAL_KEY) != "true":
            missing.append(LOCATION_KEY)
    if (
        candidate.item_type not in {"reference", "routine", "decision", "question"}
        and candidate.disposition != "decision_pending"
        and candidate.due_date is None
        and candidate.scheduled_date is None
    ):
        if "exact_date" not in missing:
            missing.append("date")
    return _dedupe(missing)


def missing_slots_for_proposal(proposal: Proposal) -> tuple[str, ...]:
    """Return machine slot names still missing from a persisted proposal."""

    missing: list[str] = []
    if proposal.assigned_to == "unassigned":
        missing.append("assigned_to")
    if proposal.metadata.get("needs_exact_date") == "true" and proposal.due_date is None and proposal.scheduled_date is None:
        missing.append("exact_date")
    if proposal.kind == "routine":
        for key, slot in (
            ("recurrence_frequency", "recurrence"),
            ("time_window", "time"),
            (LOCATION_KEY, LOCATION_KEY),
            (PARTICIPANTS_KEY, PARTICIPANTS_KEY),
        ):
            value = proposal.metadata.get(key) if key != "time_window" else proposal.time_window
            if not value or (key == "time_window" and _is_unknown_time(value)):
                missing.append(slot)
        return _dedupe(missing)
    if proposal.kind == "event" or (
        proposal.kind == "question" and proposal.scheduled_date is not None and not _is_source_question(proposal)
    ):
        if not has_participant_metadata(proposal.metadata):
            missing.append(PARTICIPANTS_KEY)
        if _requires_exact_time(proposal.metadata, proposal.time_window):
            missing.append("time")
        if not proposal.metadata.get(LOCATION_KEY) and proposal.metadata.get(LOCATION_OPTIONAL_KEY) != "true":
            missing.append(LOCATION_KEY)
    if (
        proposal.kind not in {"reference", "routine", "decision"}
        and not _is_source_question(proposal)
        and proposal.due_date is None
        and proposal.scheduled_date is None
    ):
        if "exact_date" not in missing:
            missing.append("date")
    return _dedupe(missing)


def _requires_exact_time(metadata: dict[str, str], time_window: str) -> bool:
    if metadata.get("time_optional") == "true":
        return False
    normalized = time_window.strip().lower()
    if _is_unknown_time(normalized):
        return True
    explicitly_required = metadata.get(NEEDS_EXACT_TIME_KEY) == "true"
    broad_lunch_hint = normalized in {"lunch", "noon", "점심", "점심시간", "점심 시간대"}
    return (explicitly_required or broad_lunch_hint) and not _has_exact_time(normalized)


def _has_exact_time(value: str) -> bool:
    return re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", value) is not None


def _is_unknown_time(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"?", "??", "???", "unknown", "unk", "tbd", "미정", "미확정", "정해지지 않음", "not_decided"}


def has_participant_metadata(metadata: dict[str, str]) -> bool:
    return any(metadata.get(key) for key in (PARTICIPANTS_KEY, EXTERNAL_PARTICIPANTS_KEY, PARTICIPANT_LABEL_KEY, ATTENDEES_KEY))


def _is_source_question(proposal: Proposal) -> bool:
    return proposal.kind == "question" and proposal.metadata.get("source_item_type") == "question"


def _dedupe(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))

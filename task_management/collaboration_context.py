from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import re
from typing import Sequence

from .domain import TeamTaskTaskCandidate, IncomingMessage, Proposal


def normalize_external_collaboration_candidate(
    candidate: TeamTaskTaskCandidate,
    *,
    message: IncomingMessage,
    existing_proposals: Sequence[Proposal],
) -> TeamTaskTaskCandidate:
    """Normalize LLM drafts that mention external 담당자 into internal ownership.

    The product contract only assigns tasks to internal task_management actors.  Named
    work counterparts such as "DataPortal 담당자 김담당 선생님" are therefore stored as
    external owners/participants, while the sender remains the internal owner who
    can review, remind, and approve the item locally.
    """

    candidate = _normalize_work_discussion_context(candidate, message=message)
    candidate = _normalize_tentative_decision_context(candidate, message=message)
    metadata = dict(candidate.metadata)
    if not _looks_like_external_counterpart_context(candidate, metadata):
        return candidate

    internal_owner = _internal_owner(message.sender_id)
    metadata.setdefault("internal_owner", internal_owner)
    metadata.setdefault("collaboration_context", "external_counterpart")
    external_owner = _external_owner(metadata)
    if external_owner:
        metadata.setdefault("external_owner", external_owner)
    metadata["participants"] = _csv_append(metadata.get("participants", ""), internal_owner)

    assigned_to = candidate.assigned_to
    if assigned_to == "unassigned":
        assigned_to = "me" if internal_owner == message.sender_id else internal_owner

    parent = (
        _find_review_context_parent(existing_proposals, reference_date=message.received_at.date())
        if _looks_like_followup_work_batch(message.text)
        else None
    )
    due_date = candidate.due_date
    scheduled_date = candidate.scheduled_date
    time_window = candidate.time_window
    if parent is not None and due_date is None and scheduled_date is None:
        due_date = parent.due_date
        scheduled_date = parent.scheduled_date
        time_window = time_window or parent.time_window
        metadata.setdefault("parent_proposal_id", parent.proposal_id)
        metadata.setdefault("parent_title", parent.title)
        metadata.setdefault("context_inherited_date", "true")
        metadata.setdefault("context_inherited_from", "followup_review_parent")

    return replace(
        candidate,
        assigned_to=assigned_to,
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=time_window,
        metadata=metadata,
    )


def _normalize_work_discussion_context(
    candidate: TeamTaskTaskCandidate,
    *,
    message: IncomingMessage,
) -> TeamTaskTaskCandidate:
    """Do not ask for a venue when a DM only schedules a work discussion.

    A natural-language DM such as "박연구 박사님하고 다음주 월요일 오후 3시쯤
    논의" is enough to create the discussion task.  Unless the user explicitly
    says a location is required or names a place, location should remain optional
    rather than becoming a blocking clarification.
    """

    metadata = dict(candidate.metadata)
    if metadata.get("location") or metadata.get("location_optional") == "true":
        return candidate
    haystack = f"{candidate.raw_text} {candidate.title} {message.text}"
    if _looks_like_discussion_without_required_place(haystack, metadata):
        metadata["location_optional"] = "true"
        metadata.setdefault("location_policy", "optional_for_work_discussion")
        return replace(candidate, metadata=metadata)
    return candidate


def _looks_like_discussion_without_required_place(text: str, metadata: dict[str, str]) -> bool:
    has_discussion = any(token in text for token in ("논의", "회의", "미팅", "얘기", "이야기"))
    has_counterpart = bool(metadata.get("external_participants") or metadata.get("participant_label")) or bool(
        re.search(r"[가-힣]{2,4}\s*(?:박사님|선생님|교수님|님)", text)
    )
    explicit_location_need = any(token in text for token in ("장소 필요", "장소가 필요", "장소 정", "장소 미정"))
    physical_place = bool(re.search(r"(?:회의실|식당|카페|고객 미팅|회의|학교|센터|건물|층)", text))
    return has_discussion and has_counterpart and not explicit_location_need and not physical_place


def _normalize_tentative_decision_context(
    candidate: TeamTaskTaskCandidate,
    *,
    message: IncomingMessage,
) -> TeamTaskTaskCandidate:
    """Treat tentative future dates decided in a scheduled discussion as notes.

    If the message says the actual schedule will be discussed elsewhere, a
    candidate like "study-group는 6월 초에 할듯" should not immediately ask for an
    exact date/time/location.  It is a decision-pending planning note attached
    to the scheduled discussion context.
    """

    metadata = dict(candidate.metadata)
    haystack = f"{candidate.raw_text} {candidate.title}"
    full_text = message.text
    has_tentative_window = bool(metadata.get("date_window_start") and metadata.get("date_window_end")) and any(
        token in haystack for token in ("예상", "할듯", "쯤", "초", "중순", "말", "미정")
    )
    schedule_will_be_decided = any(token in full_text for token in ("일정 논의", "일정도", "논의도", "진행할 것", "결정"))
    if not (has_tentative_window and schedule_will_be_decided):
        return candidate
    metadata.pop("needs_exact_date", None)
    metadata.pop("needs_exact_time", None)
    metadata["date_resolution_policy"] = "decide_in_scheduled_discussion"
    metadata["decision_pending"] = "true"
    metadata["time_optional"] = "true"
    metadata["location_optional"] = "true"
    metadata.setdefault("floating_reason", "future_schedule_to_be_decided_in_discussion")
    return replace(
        candidate,
        item_type="decision",
        disposition="decision_pending",
        time_window="",
        metadata=metadata,
    )


def _looks_like_external_counterpart_context(candidate: TeamTaskTaskCandidate, metadata: dict[str, str]) -> bool:
    haystack = " ".join(
        value
        for value in (
            candidate.raw_text,
            candidate.title,
            metadata.get("participant_label", ""),
            metadata.get("external_participants", ""),
            metadata.get("external_owner", ""),
        )
        if value
    )
    return "담당자" in haystack and bool(
        metadata.get("external_participants") or re.search(r"[가-힣]{2,4}\s*선생님", haystack)
    )


def _internal_owner(sender_id: str) -> str:
    return sender_id if sender_id in {"me", "teammate"} else "me"


def _external_owner(metadata: dict[str, str]) -> str:
    for value in (metadata.get("participant_label", ""), metadata.get("external_participants", "")):
        owner = _first_teacher_name_after_owner_marker(value)
        if owner:
            return owner
    return ""


def _first_teacher_name_after_owner_marker(value: str) -> str:
    if not value:
        return ""
    candidate = value
    if "담당자" in candidate:
        candidate = candidate.split("담당자", 1)[1]
    match = re.search(r"([가-힣]{2,4})\s*선생님", candidate)
    if match:
        return f"{match.group(1)} 선생님"
    first = re.split(r"[,/;]", candidate)[0].strip()
    return first


def _csv_append(csv: str, value: str) -> str:
    parts = [item.strip() for item in csv.split(",") if item.strip()]
    if value not in parts:
        parts.append(value)
    return ",".join(parts)


def _find_review_context_parent(proposals: Sequence[Proposal], *, reference_date: date) -> Proposal | None:
    candidates = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied", "awaiting_approval"}
        and proposal.kind in {"task", "event", "question"}
        and _has_review_context(proposal)
        and _proposal_date(proposal) is not None
        and _proposal_date(proposal) >= reference_date
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda proposal: (
            0 if "복귀" in f"{proposal.title} {proposal.raw_text}" else 1,
            _proposal_date(proposal) or date.max,
            proposal.updated_at or proposal.created_at or datetime.max,
        )
    )
    return candidates[0]


def _looks_like_followup_work_batch(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    mentions_counterparts = "담당자들" in compact or compact.count("담당자") >= 2
    mentions_work_summary = any(token in compact for token in ("할일", "할일정리", "정리했", "정리함", "정리"))
    return mentions_counterparts and mentions_work_summary


def _has_review_context(proposal: Proposal) -> bool:
    haystack = f"{proposal.title} {proposal.raw_text}"
    return ("복귀" in haystack or "차주" in haystack) and any(token in haystack for token in ("확인", "검토", "정리"))


def _proposal_date(proposal: Proposal) -> date | None:
    return proposal.due_date or proposal.scheduled_date

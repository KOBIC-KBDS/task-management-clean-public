from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import os
from typing import Iterable

from .domain import Proposal


ACTOR_LABELS = {
    "me": "사용자님",
    "teammate": "팀원님",
    "shared": "팀 공동",
    "unassigned": "담당자 미정",
}

STATUS_LABELS = {
    "draft": "초안",
    "posted": "게시됨",
    "awaiting_approval": "승인 대기",
    "approved": "승인됨",
    "rejected": "거절됨",
    "applied": "적용됨",
    "done": "완료",
}

STATUS_ICONS = {
    "draft": "▫️",
    "posted": "▫️",
    "awaiting_approval": "",
    "approved": "☐",
    "rejected": "⛔",
    "applied": "✅",
    "done": "✅",
}

MISSING_SLOT_LABELS = {
    "date": "일시",
    "exact_date": "정확한 날짜",
    "time": "정확한 시간",
    "location": "장소",
    "participants": "참여자",
    "assigned_to": "담당자",
    "recurrence": "반복 주기",
}


@dataclass(frozen=True)
class HumanSlotQuestion:
    """A channel-neutral question for missing proposal slots."""

    proposal_id: str
    request_id: str
    title: str
    assignee_label: str
    missing_slot_labels: tuple[str, ...]
    known_when: str = ""

    @property
    def bold_missing_slots(self) -> str:
        return ", ".join(f"*{label}*" for label in self.missing_slot_labels)


def build_missing_slot_question(proposal: Proposal, *, request_id: str = "") -> HumanSlotQuestion:
    missing = tuple(missing_slot_label(slot) for slot in proposal.missing_slots) or ("확인 사항",)
    return HumanSlotQuestion(
        proposal_id=proposal.proposal_id,
        request_id=request_id,
        title=proposal.title,
        assignee_label=actor_label(proposal.assigned_to),
        missing_slot_labels=missing,
        known_when=human_when_label(proposal),
    )


def render_missing_slot_sentence(question: HumanSlotQuestion) -> str:
    known_when = f" 현재 잡힌 일정 후보는 {question.known_when}입니다." if question.known_when else ""
    request = (
        f" 답장은 `변경 {question.request_id} ...` 형태로 주세요."
        if question.request_id
        else " 답장으로 필요한 정보를 알려주세요."
    )
    missing = question.bold_missing_slots
    return (
        f"{question.title} 담당자는 {question.assignee_label}으로 잡혀 있지만 "
        f"아직 정해지지 않은 정보가 있습니다: {missing}."
        f"{known_when} {missing} 정보를 알려주세요.{request}"
    )


def render_missing_slot_reminder(question: HumanSlotQuestion) -> str:
    example_request = question.request_id or "approval/xxxx"
    known_when = f"\n현재 잡힌 일정 후보는 {question.known_when}입니다." if question.known_when else ""
    return (
        "*확인이 필요한 항목입니다.*\n"
        f"{question.title} 담당자는 {question.assignee_label}으로 잡혀 있지만 "
        f"아직 정해지지 않은 정보가 있습니다: {question.bold_missing_slots}."
        f"{known_when}\n"
        f"{question.bold_missing_slots} 정보를 알려주세요. 예: `변경 {example_request} 다음 주 화요일 오전`"
    )


def render_confirmed_sentence(proposal: Proposal, *, include_short_id: bool = True) -> str:
    title = proposal.title
    assignee = actor_label(proposal.assigned_to)
    when = human_when_label(proposal)
    location = proposal.metadata.get("location", "")
    participants = proposal_participants_label(proposal)
    participant_sentence = f" 관련자는 {participants}입니다." if participants and participants != assignee else ""
    prep_parent = proposal.metadata.get("parent_proposal_id", "")
    suffix = f" `{short_id(proposal.proposal_id)}`" if include_short_id else ""
    if proposal.metadata.get("link_type") == "prep_subtask":
        prefix = due_when_prefix(when)
        parent = " 관련 준비 작업" if prep_parent else ""
        target = object_phrase(f"{title}{parent}")
        return f"{prefix}{target} 완료해야 합니다. 담당자는 {assignee}입니다.{suffix}"
    if proposal.kind == "event" or proposal.scheduled_date is not None:
        prefix = f"{when}에 " if when else ""
        sentence = f"{prefix}{title} 일정이 예정되어 있습니다. 담당자는 {assignee}입니다.{participant_sentence}"
        if location:
            sentence += f" 장소는 {location}입니다."
        return f"{sentence}{suffix}"
    prefix = due_when_prefix(when)
    target = object_phrase(title)
    return f"{prefix}{target} 진행하면 됩니다. 담당자는 {assignee}입니다.{participant_sentence}{suffix}"


def human_when_label(proposal: Proposal | None) -> str:
    if proposal is None:
        return ""
    parts = []
    proposal_date = proposal.scheduled_date or proposal.due_date
    if proposal_date:
        parts.append(date_label(proposal_date))
    if proposal.time_window:
        parts.append(time_label(proposal.time_window))
    if proposal.metadata.get("date_window_start") and proposal.metadata.get("date_window_end"):
        start = date.fromisoformat(proposal.metadata["date_window_start"])
        end = date.fromisoformat(proposal.metadata["date_window_end"])
        parts.append(f"{date_range_label(start, end)} 중")
    return " ".join(parts)


WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")


def weekday_label(value: date) -> str:
    return WEEKDAY_LABELS[value.weekday()]


def date_label(value: date) -> str:
    return f"{value.year}년 {value.month}월 {value.day}일({weekday_label(value)})"


def datetime_label(value: datetime) -> str:
    return f"{date_label(value.date())} {value:%H:%M}"


def date_range_label(start: date, end: date) -> str:
    return f"{date_label(start)}~{date_label(end)}"


def date_window_display_label(start: str, end: str, label: str = "") -> str:
    if start and end:
        try:
            range_label = date_range_label(date.fromisoformat(start), date.fromisoformat(end))
        except ValueError:
            range_label = f"{start}..{end}"
        return f"{label} ({range_label})" if label else range_label
    return label


def time_label(value: str) -> str:
    return {
        "morning": "오전",
        "afternoon": "오후",
        "evening": "저녁",
        "lunch": "점심 시간대",
        "noon": "점심 시간대",
        "점심": "점심 시간대",
    }.get(value, value)


def due_when_prefix(when: str) -> str:
    if not when:
        return ""
    if when.endswith("까지") or when.endswith("전에") or when.endswith(" 전") or when.endswith(" 중") or when == "중":
        return f"{when} "
    return f"{when}까지 "


def missing_slot_label(value: str) -> str:
    return MISSING_SLOT_LABELS.get(value, value)


def missing_slot_labels(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(missing_slot_label(value) for value in values if value)


def render_missing_slot_labels(values: Iterable[str]) -> str:
    return ", ".join(missing_slot_labels(values))


def actor_label(actor_id: str) -> str:
    if actor_id == "me":
        return os.environ.get("TASK_MANAGEMENT_ACTOR_LABEL_ME", ACTOR_LABELS["me"])
    return ACTOR_LABELS.get(actor_id, actor_id)


def proposal_status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def proposal_status_icon(status: str) -> str:
    return STATUS_ICONS.get(status, "▫️")


def actor_list_label(value: str) -> str:
    people = [item.strip() for item in value.split(",") if item.strip()]
    return ", ".join(actor_label(item) for item in people)


def proposal_participants_label(proposal: Proposal) -> str:
    metadata = proposal.metadata
    if metadata.get("participant_label"):
        return metadata["participant_label"]
    if metadata.get("attendees"):
        return metadata["attendees"]
    participants = actor_list_label(metadata.get("participants", ""))
    external = metadata.get("external_participants", "")
    if participants and external:
        return f"{participants} + {external}"
    return participants or external


def short_id(value: str) -> str:
    return value.rsplit("/", 1)[-1][:12]


def object_phrase(value: str) -> str:
    return f"{value}{object_particle(value)}"


def object_particle(value: str) -> str:
    """Return Korean object particle for the last Hangul syllable when possible."""

    stripped = value.strip()
    if not stripped:
        return "을"
    code = ord(stripped[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return "을" if (code - 0xAC00) % 28 else "를"
    return "을"

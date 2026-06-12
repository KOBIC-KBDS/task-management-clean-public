from __future__ import annotations

from datetime import date, timedelta
import hashlib
import re
from typing import Iterable

from .domain import TeamTaskMessage, TeamTaskTaskCandidate


SPEAKER_RE = re.compile(r"^(?P<speaker>[^:：]{1,24})[:：]\s*(?P<text>.+)$")
URL_RE = re.compile(r"https?://\S+")
ISO_DATE_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
WEEKDAY_INDEX = {
    "월": 0,
    "화": 1,
    "수": 2,
    "목": 3,
    "금": 4,
    "토": 5,
    "일": 6,
}
AREA_KEYWORDS = (
    ("work", ("회의", "교육", "학교", "학원", "프로젝트", "공유폴더", "보고서", "자료", "준비 자료", "서류", "참석", "워크숍")),
    ("health", ("고객 미팅", "진료", "검진", "약", "처방", "예약")),
    ("chores", ("청소", "빨래", "설거지", "분리수거", "쓰레기", "장보기", "정리")),
    ("finance", ("납부", "결제", "은행", "보험", "돈", "영수증")),
    ("team", ("팀", "협업자", "협업자", "협업자", "팀 행사")),
)
ACTION_HINTS = (
    "해줘",
    "해주세요",
    "챙겨",
    "챙길게",
    "확인",
    "예약",
    "잡아",
    "보내",
    "사줘",
    "정리",
    "부탁",
    "해야",
    "하자",
    "물어봐",
    "가자",
    "할게",
    "참석",
    "가능",
    "좋겠어",
    "가야",
    "가야함",
    "가야 해",
    "미팅",
    "회의",
    "참석",
    "회식",
    "준비",
    "필요",
    "해야겠다",
    "해야겠",
    "해야돼",
    "해야 해",
    "출장",
    "입과",
    "교육",
)
NEGATIVE_HINTS = ("하지 말자", "하지마", "안 하기로", "취소하자", "필요 없어")
DONE_HINTS = ("이미 했어", "완료", "끝냈", "처리했", "해뒀")
REVIEW_HINTS = ("나중에", "언젠가", "검토", "논의", "정하자", "상담")


def parse_manual_discussion(
    text: str,
    *,
    discussion_id: str | None = None,
    reference_date: date | None = None,
    source_url: str = "",
    source_export_path: str = "",
) -> tuple[TeamTaskTaskCandidate, ...]:
    """Parse manual task_management discussion text into preview candidates."""

    today = reference_date or date.today()
    resolved_discussion_id = discussion_id or f"manual/{today.isoformat()}"
    candidates: list[TeamTaskTaskCandidate] = []
    for message in _iter_messages(text, discussion_id=resolved_discussion_id):
        for segment_index, segment in enumerate(_split_segments(message.text), start=1):
            candidate = _segment_to_candidate(
                message,
                segment,
                segment_index=segment_index,
                reference_date=today,
                source_url=source_url,
                source_export_path=source_export_path,
            )
            if candidate is not None:
                candidates.append(candidate)
    return tuple(candidates)


def parse_temporal_update(text: str, *, reference_date: date) -> dict[str, str]:
    """Parse a short date/time correction command body."""

    due_date, scheduled_date, time_window = _extract_dates(text, reference_date=reference_date)
    date_window = _extract_date_window(text, reference_date=reference_date)
    update: dict[str, str] = {}
    if due_date is not None:
        update["due_date"] = due_date.isoformat()
    if scheduled_date is not None:
        update["scheduled_date"] = scheduled_date.isoformat()
    if time_window:
        update["time_window"] = time_window
    update.update(date_window)
    update.update(_extract_feedback_metadata(text, reference_date=reference_date))
    return update


def _iter_messages(text: str, *, discussion_id: str) -> Iterable[TeamTaskMessage]:
    message_number = 0
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        message_number += 1
        speaker = ""
        body = line
        match = None if URL_RE.match(line) else SPEAKER_RE.match(line)
        if match:
            speaker = match.group("speaker").strip()
            body = match.group("text").strip()
        yield TeamTaskMessage(
            discussion_id=discussion_id,
            message_id=f"{discussion_id}/{message_number}",
            line_number=line_number,
            speaker=speaker,
            text=body,
        )


def _split_segments(text: str) -> tuple[str, ...]:
    if URL_RE.fullmatch(text.strip()):
        return (text.strip(),)
    parts = re.split(r"(?<=[.!?。])\s+|[;\n]+", text)
    return tuple(_clean_sentence(part) for part in parts if _clean_sentence(part))


def _segment_to_candidate(
    message: TeamTaskMessage,
    segment: str,
    *,
    segment_index: int,
    reference_date: date,
    source_url: str,
    source_export_path: str,
) -> TeamTaskTaskCandidate | None:
    if _is_negative(segment):
        return None

    urls = URL_RE.findall(segment)
    has_action = _has_action_hint(segment)
    if urls and not has_action:
        title = f"참고 링크 확인: {urls[0]}"
        source_key = _source_key(message.message_id, segment_index, title)
        return TeamTaskTaskCandidate(
            source_key=source_key,
            raw_text=title,
            title=title,
            discussion_id=message.discussion_id,
            message_id=f"{message.message_id}/{segment_index}",
            line_number=message.line_number,
            speaker=message.speaker,
            assigned_to="shared",
            task_management_area=_infer_area(segment),
            item_type="reference",
            disposition="reference",
            source_url=source_url,
            source_export_path=source_export_path,
            metadata={"source_urls": ",".join(urls)},
        )

    if not has_action:
        return None

    due_date, scheduled_date, time_window = _extract_dates(segment, reference_date=reference_date)
    date_window = _extract_date_window(segment, reference_date=reference_date)
    metadata = {
        **date_window,
        **_extract_routine_metadata(segment, reference_date=reference_date),
        **_extract_feedback_metadata(segment, reference_date=reference_date),
    }
    if _is_blocking_away_context(segment):
        metadata.setdefault("participants", "me")
        metadata.setdefault("event_scope", "away")
        metadata.setdefault("blocks_in_person", "true")
        metadata.setdefault("location_optional", "true")
        if not time_window or (
            date_window.get("date_window_kind") == "range" and time_window in {"lunch", "noon", "점심"}
        ):
            time_window = "all_day"
    if date_window.get("date_window_kind") == "range" and date_window.get("date_window_start"):
        scheduled_date = date.fromisoformat(date_window["date_window_start"])
    title = _title_from_segment(segment)
    if not title:
        return None
    task_status = "done" if any(hint in segment for hint in DONE_HINTS) else "active"
    needs_review = any(hint in segment for hint in REVIEW_HINTS)
    source_key = _source_key(message.message_id, segment_index, title)
    assigned_to = _infer_assignee(segment, speaker=message.speaker)
    if assigned_to == "unassigned":
        assigned_to = _assignee_from_participants(metadata.get("participants", ""))
    item_type = _infer_item_type(segment, metadata=metadata, scheduled_date=scheduled_date)
    if item_type == "event" and assigned_to in {"me", "teammate"} and not any(
        metadata.get(key) for key in ("participants", "external_participants", "participant_label", "attendees")
    ):
        metadata["participants"] = assigned_to
    if item_type == "event" and time_window in {"lunch", "noon", "점심"}:
        metadata["needs_exact_time"] = "true"
    return TeamTaskTaskCandidate(
        source_key=source_key,
        raw_text=title,
        title=title,
        discussion_id=message.discussion_id,
        message_id=f"{message.message_id}/{segment_index}",
        line_number=message.line_number,
        speaker=message.speaker,
        assigned_to=assigned_to,
        task_management_area=_infer_area(segment),
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=time_window,
        task_status=task_status,
        needs_review=needs_review,
        source_url=source_url,
        source_export_path=source_export_path,
        item_type=item_type,
        metadata=metadata,
    )


def _clean_sentence(text: str) -> str:
    return text.strip().strip("-•* ").strip()


def _is_negative(text: str) -> bool:
    return any(hint in text for hint in NEGATIVE_HINTS)


def _has_action_hint(text: str) -> bool:
    return any(hint in text for hint in ACTION_HINTS)


def _infer_area(text: str) -> str:
    for area, keywords in AREA_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return area
    return "general"


def _infer_assignee(text: str, *, speaker: str) -> str:
    normalized_speaker = speaker.strip()
    participants = _extract_participants(text)
    if set(participants) >= {"me", "teammate"}:
        return "shared"
    if participants == ("me",):
        return _speaker_person(normalized_speaker)
    if participants == ("teammate",):
        return "teammate"
    if "워크숍" in text:
        return "shared"
    if "팀원" in text or "팀원" in text or "팀원" in text:
        return "teammate"
    if "나" in text or "팀원" in text:
        return "me"
    if "우리" in text or "같이" in text:
        return "shared"
    if "당신" in text or "여보" in text:
        return _other_person(normalized_speaker)
    if "내가" in text or "제가" in text or "할게" in text or "챙길게" in text:
        return _speaker_person(normalized_speaker)
    if any(token in text for token in ("해야겠다", "해야겠", "해야돼")):
        return _speaker_person(normalized_speaker)
    if _is_blocking_away_context(text):
        return _speaker_person(normalized_speaker)
    if _is_event_context(text):
        return _speaker_person(normalized_speaker)
    if "해줘" in text or "해주세요" in text or "사줘" in text or "잡아줘" in text:
        return _other_person(normalized_speaker)
    return "unassigned"


def _speaker_person(speaker: str) -> str:
    if not speaker:
        return "me"
    if speaker in {"나", "저", "me"}:
        return "me"
    if speaker in {"팀원", "팀원", "팀원", "팀원", "나", "teammate"}:
        return "teammate"
    return "unassigned"


def _other_person(speaker: str) -> str:
    if speaker in {"나", "저", "me"}:
        return "teammate"
    if speaker in {"팀원", "팀원", "팀원", "팀원", "나", "teammate"}:
        return "me"
    return "unassigned"


def _extract_dates(text: str, *, reference_date: date) -> tuple[date | None, date | None, str]:
    if _extract_date_window(text, reference_date=reference_date):
        return None, None, _extract_time_window(text)
    explicit = ISO_DATE_RE.search(text)
    parsed_date: date | None = None
    if explicit:
        parsed_date = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
    else:
        parsed_date = _relative_date(text, reference_date=reference_date)

    time_window = _extract_time_window(text)

    if parsed_date is None:
        if time_window and _is_event_context(text):
            return None, reference_date, time_window
        return None, None, time_window
    if _is_event_context(text) or "오전" in text or "오후" in text or "저녁" in text:
        return None, parsed_date, time_window
    return parsed_date, None, time_window


def _extract_time_window(text: str) -> str:
    clock = re.search(r"(?:(오전|오후|저녁|밤)\s*)?(\d{1,2})\s*시(?:\s*(?:(\d{1,2})\s*분|반))?", text)
    if clock:
        hour = int(clock.group(2))
        minute = 30 if "반" in clock.group(0) and clock.group(3) is None else int(clock.group(3) or 0)
        marker = clock.group(1) or ""
        if marker in {"오후", "저녁", "밤"} and hour < 12:
            hour += 12
        if not marker and hour <= 7 and any(token in text for token in ("퇴근", "저녁", "밤")):
            hour += 12
        if not marker and hour <= 7 and _is_event_context(text):
            hour += 12
        if marker == "오전" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    if "오전" in text:
        return "morning"
    if "오후" in text:
        return "afternoon"
    if "저녁" in text or "밤" in text:
        return "evening"
    if "점심" in text:
        return "lunch"
    return ""


def _extract_date_window(text: str, *, reference_date: date) -> dict[str, str]:
    week_range = re.search(
        r"(이번\s*주|이번주|다음\s*주|다음주)\s*([월화수목금토일])(?:요일)?\s*(?:~|-|부터)\s*([월화수목금토일])(?:요일)?",
        text,
    )
    if week_range:
        base_monday = reference_date - timedelta(days=reference_date.weekday())
        if "다음" in week_range.group(1):
            base_monday += timedelta(days=7)
        start = base_monday + timedelta(days=WEEKDAY_INDEX[week_range.group(2)])
        end = base_monday + timedelta(days=WEEKDAY_INDEX[week_range.group(3)])
        if end < start:
            end += timedelta(days=7)
        is_choice_window = "중" in text and "하루" in text
        return {
            "date_window_start": start.isoformat(),
            "date_window_end": end.isoformat(),
            "date_window_label": week_range.group(0),
            "date_window_kind": "choice" if is_choice_window else "range",
            **({"needs_exact_date": "true"} if is_choice_window else {}),
        }

    plain_day_range = re.search(
        r"(^|\s)([월화수목금토일])(?:요일)?\s*(?:~|-|부터)\s*([월화수목금토일])(?:요일)?(?=$|\s|에|은|는|이라|까지)",
        text,
    )
    if plain_day_range:
        base_monday = reference_date - timedelta(days=reference_date.weekday())
        start = base_monday + timedelta(days=WEEKDAY_INDEX[plain_day_range.group(2)])
        end = base_monday + timedelta(days=WEEKDAY_INDEX[plain_day_range.group(3)])
        if start < reference_date and "이번" not in text:
            start += timedelta(days=7)
            end += timedelta(days=7)
        if end < start:
            end += timedelta(days=7)
        is_choice_window = "중" in text and "하루" in text
        return {
            "date_window_start": start.isoformat(),
            "date_window_end": end.isoformat(),
            "date_window_label": plain_day_range.group(0).strip(),
            "date_window_kind": "choice" if is_choice_window else "range",
            **({"needs_exact_date": "true"} if is_choice_window else {}),
        }

    if "주말" in text:
        base_monday = reference_date - timedelta(days=reference_date.weekday())
        if "다음" in text:
            base_monday += timedelta(days=7)
        start = base_monday + timedelta(days=5)
        end = base_monday + timedelta(days=6)
        return {
            "date_window_start": start.isoformat(),
            "date_window_end": end.isoformat(),
            "date_window_label": "주말",
            "date_window_kind": "choice",
            "needs_exact_date": "true",
        }
    return {}


def _relative_date(text: str, *, reference_date: date) -> date | None:
    if re.search(r"이번\s*주\s*(안에|까지)", text):
        base_monday = reference_date - timedelta(days=reference_date.weekday())
        return base_monday + timedelta(days=6)
    if re.search(r"다음\s*주\s*(안에|까지)", text):
        base_monday = reference_date - timedelta(days=reference_date.weekday()) + timedelta(days=7)
        return base_monday + timedelta(days=6)
    if "오늘" in text:
        return reference_date
    if "내일" in text:
        return reference_date + timedelta(days=1)
    if "모레" in text:
        return reference_date + timedelta(days=2)

    week_match = re.search(r"(이번\s*주|이번주|다음\s*주|다음주|차주)\s*([월화수목금토일])", text)
    if week_match:
        base_monday = reference_date - timedelta(days=reference_date.weekday())
        if "다음" in week_match.group(1) or "차주" in week_match.group(1):
            base_monday += timedelta(days=7)
        return base_monday + timedelta(days=WEEKDAY_INDEX[week_match.group(2)])

    plain_weekday = re.search(r"(^|\s)([월화수목금토일])(?:요일)(?=$|\s|에|오전|오후|저녁|밤)", text)
    if plain_weekday:
        return _next_weekday(reference_date, WEEKDAY_INDEX[plain_weekday.group(2)])
    return None


def _title_from_segment(segment: str) -> str:
    title = segment
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"(이번\s*주|이번주|다음\s*주|다음주)\s*[월화수목금토일](?:요일)?\s*(?:~|-|부터)\s*[월화수목금토일](?:요일)?\s*(중\s*하루)?", "", title)
    title = re.sub(r"(^|\s)[월화수목금토일](?:요일)?\s*(?:~|-|부터)\s*[월화수목금토일](?:요일)?(?=$|\s|에|은|는|이라|까지)", " ", title)
    title = re.sub(r"(이번\s*주|이번주|다음\s*주|다음주)\s*(안에|까지)", "", title)
    title = re.sub(r"(오늘|내일|모레|이번\s*주|이번주|다음\s*주|다음주)\s*[월화수목금토일]?(까지|으로|에)?", "", title)
    title = re.sub(r"\d{4}-\d{2}-\d{2}(까지|으로|에)?", "", title)
    title = re.sub(r"(?:(?:오전|오후|저녁|밤)\s*)?\d{1,2}\s*시(?:\s*(?:\d{1,2}\s*분|반))?\s*(?:에|부터)?", "", title)
    title = title.replace("오전으로", "").replace("오후로", "").replace("저녁으로", "")
    title = re.sub(r"^(내가|제가|당신은|당신이|여보가|우리|같이)\s*", "", title)
    replacements = (
        ("챙겨줘", "챙기기"),
        ("챙길게", "챙기기"),
        ("확인해줘", "확인"),
        ("잡아줘", "잡기"),
        ("예약 잡기", "예약"),
        ("해주세요", "하기"),
        ("해줘", "하기"),
        ("할게", "하기"),
        ("하자", "하기"),
        ("가야함", "가기"),
        ("가야 해", "가기"),
        ("가야", "가기"),
    )
    for old, new in replacements:
        title = title.replace(old, new)
    title = title.strip(" .!?,。")
    title = re.sub(r"\s+", " ", title)
    return title


def _source_key(message_id: str, segment_index: int, title: str) -> str:
    normalized_title = re.sub(r"\s+", " ", title.strip())
    digest_source = f"{message_id}:{segment_index}:{normalized_title}"
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]
    return f"task_management/{digest}"


def _extract_feedback_metadata(text: str, *, reference_date: date) -> dict[str, str]:
    metadata: dict[str, str] = {}
    participants = _extract_participants(text)
    if participants:
        metadata["participants"] = ",".join(participants)
    location = _extract_location(text)
    if location:
        metadata["location"] = location
    if "장소" in text and any(token in text for token in ("필요하지", "필요 없어", "필요없", "상관없")):
        metadata["location_optional"] = "true"
    deferred_slots = _extract_deferred_slots(text)
    if deferred_slots:
        metadata["defer_missing_slots"] = ",".join(deferred_slots)
    materials = _extract_materials(text)
    if materials:
        metadata["materials"] = materials
        metadata["needs_prep"] = "true"
    return metadata


def _extract_routine_metadata(text: str, *, reference_date: date) -> dict[str, str]:
    if not _is_routine_context(text):
        return {}
    weekday = _routine_weekday(text)
    if weekday is None:
        # Default to the reference weekday only when the message clearly says weekly.
        weekday = reference_date.weekday()
    next_occurrence = _next_weekday(reference_date, weekday)
    if next_occurrence < reference_date:
        next_occurrence += timedelta(days=7)
    return {
        "recurrence_frequency": "weekly",
        "recurrence_weekday": str(weekday),
        "next_occurrence_date": next_occurrence.isoformat(),
    }


def _extract_participants(text: str) -> tuple[str, ...]:
    normalized = text.replace(" ", "")
    participants: list[str] = []
    if any(token in normalized for token in ("나랑", "나와", "내가", "저랑", "저와", "참석자나", "참석자는나", "me")):
        participants.append("me")
    if any(token in normalized for token in ("팀원", "팀원", "팀원", "teammate")):
        participants.append("teammate")
    if any(token in normalized for token in ("우리", "팀전체", "팀공동", "팀모두", "모두같이", "전체참석")):
        participants.extend(["me", "teammate"])
    if "나혼자" in normalized or "혼자" in normalized:
        participants = ["me"]
    return tuple(dict.fromkeys(participants))


def _assignee_from_participants(participants: str) -> str:
    people = tuple(item for item in participants.split(",") if item)
    if set(people) >= {"me", "teammate"}:
        return "shared"
    if people == ("me",):
        return "me"
    if people == ("teammate",):
        return "teammate"
    return "unassigned"


def _extract_location(text: str) -> str:
    explicit = re.search(r"장소(?:는|:)?\s*([^,.。\n]+)", text)
    if explicit:
        location = explicit.group(1).strip(" ,.")
        if location and not any(token in location for token in ("필요하지", "필요 없어", "필요없", "상관없")):
            return location
    after_clock_place = re.search(
        r"(?:(?:오전|오후|저녁|밤)\s*)?\d{1,2}\s*시(?:\s*(?:\d{1,2}\s*분|반))?\s*(?:에|부터)?\s*([^,.。\n]{1,30}?)(?:에서|에)\s*",
        text,
    )
    if after_clock_place:
        location = after_clock_place.group(1).strip(" ,.")
        if location and not _looks_like_time_suffix(location):
            return location
    after_clock = re.search(
        r"(?:(?:오전|오후|저녁|밤)\s*)?\d{1,2}\s*시(?:\s*(?:\d{1,2}\s*분|반))?\s*([^,.。\n]+)",
        text,
    )
    if after_clock:
        location = after_clock.group(1).strip(" ,.")
        if location and not _looks_like_time_suffix(location):
            return location
    room = re.search(r"(\d+\s*층\s*회의실)", text)
    if room:
        return room.group(1).strip(" ,.")
    room = re.search(r"([가-힣A-Za-z0-9 -]{1,20}회의실)", text)
    if room:
        return room.group(1).strip(" ,.")
    for token in ("워크숍", "고객 미팅", "회의", "교육", "학교", "집"):
        if token in text:
            return token
    return ""


def _looks_like_time_suffix(text: str) -> bool:
    normalized = text.strip(" )]}.，,。").replace(" ", "")
    if not normalized:
        return True
    return normalized.startswith(("전", "전에", "까지", "부터", "경", "쯤")) or any(
        token in normalized
        for token in (
            "할일",
            "해야",
            "담당",
            "일시",
            "정해",
            "좋겠",
            "입니다",
            "이야",
        )
    )


def _extract_materials(text: str) -> str:
    if "자료" in text and "준비" in text:
        return "자료 준비"
    if "준비 자료" in text:
        return "준비 자료"
    if "챙" in text:
        return "챙길 것"
    return ""


def _is_routine_context(text: str) -> bool:
    lowered = text.lower()
    return (
        "주 1회" in text
        or "매주" in text
        or "weekly" in lowered
        or "루틴" in text
        or (("미팅" in text or "회의" in text) and ("주" in text or "매" in text))
    )


def _routine_weekday(text: str) -> int | None:
    match = re.search(r"([월화수목금토일])(?:요일)", text)
    if match:
        return WEEKDAY_INDEX[match.group(1)]
    return None


def _is_event_context(text: str) -> bool:
    return any(token in text for token in ("예약", "미팅", "회의", "회식", "참석", "가야", "워크숍", "참석", "출장", "입과", "교육"))


def _is_blocking_away_context(text: str) -> bool:
    return any(token in text for token in ("출장", "입과", "외근", "교육"))


def _extract_deferred_slots(text: str) -> tuple[str, ...]:
    if not any(token in text for token in ("나중에", "정해지면", "아직", "미정")):
        return ()
    slots: list[str] = []
    if any(token in text for token in ("시간", "시간대", "몇 시", "몇시")):
        slots.append("time")
    if "장소" in text and not any(token in text for token in ("필요하지", "필요 없어", "필요없", "상관없")):
        slots.append("location")
    if any(token in text for token in ("날짜", "일자", "일시", "언제")):
        slots.append("date")
    return tuple(dict.fromkeys(slots))


def _infer_item_type(text: str, *, metadata: dict[str, str], scheduled_date: date | None) -> str:
    if _is_routine_context(text) or metadata.get("recurrence_frequency"):
        return "routine"
    if scheduled_date is not None or _is_event_context(text):
        return "event"
    return "task"


def _next_weekday(reference_date: date, weekday: int) -> date:
    days_ahead = weekday - reference_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return reference_date + timedelta(days=days_ahead)

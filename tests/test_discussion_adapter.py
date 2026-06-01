from __future__ import annotations

from datetime import date

from task_management.discussion_adapter import parse_manual_discussion, parse_temporal_update


SAMPLE_DISCUSSION = """나: 이번 주 금요일까지 보고서 자료 챙겨줘.
팀원: 내가 공유폴더에 챙길게. 당신은 서류 확인해줘.
나: 고객 미팅 예약은 다음주 화요일 오전으로 잡아줘.
"""


def test_manual_discussion_parses_task_management_tasks_and_metadata() -> None:
    candidates = parse_manual_discussion(
        SAMPLE_DISCUSSION,
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )

    assert len(candidates) >= 3
    assert {candidate.task_management_area for candidate in candidates} >= {"work", "health"}
    assert {candidate.assigned_to for candidate in candidates} >= {"teammate", "me"}

    work = [candidate for candidate in candidates if candidate.task_management_area == "work"]
    assert any(candidate.due_date == date(2026, 5, 8) for candidate in work)
    assert any(candidate.assigned_to == "teammate" and "공유폴더" in candidate.title for candidate in work)
    assert any(candidate.assigned_to == "me" and "서류" in candidate.title for candidate in work)

    health = next(candidate for candidate in candidates if candidate.task_management_area == "health")
    assert health.scheduled_date == date(2026, 5, 12)
    assert health.time_window == "morning"


def test_same_manual_message_produces_stable_ids() -> None:
    first = parse_manual_discussion(
        SAMPLE_DISCUSSION,
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )
    second = parse_manual_discussion(
        SAMPLE_DISCUSSION,
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )

    assert [candidate.message_id for candidate in first] == [candidate.message_id for candidate in second]
    assert [candidate.source_key for candidate in first] == [candidate.source_key for candidate in second]


def test_url_only_message_becomes_reference_not_execution_task() -> None:
    candidates = parse_manual_discussion(
        "나: https://youtu.be/example",
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )

    assert len(candidates) == 1
    assert candidates[0].item_type == "reference"
    assert candidates[0].disposition == "reference"


def test_weekday_range_becomes_date_window_metadata() -> None:
    candidates = parse_manual_discussion(
        "나: 워크숍 이번주 목~일 중 하루 가야함",
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.assigned_to == "shared"
    assert candidate.task_management_area == "work"
    assert candidate.due_date is None
    assert candidate.scheduled_date is None
    assert candidate.metadata["date_window_start"] == "2026-05-07"
    assert candidate.metadata["date_window_end"] == "2026-05-10"
    assert candidate.metadata["needs_exact_date"] == "true"


def test_lunch_event_keeps_broad_time_as_missing_exact_time() -> None:
    candidates = parse_manual_discussion(
        "수요일 점심회식 참석자 나",
        discussion_id="manual/2026-05-18",
        reference_date=date(2026, 5, 18),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.item_type == "event"
    assert candidate.assigned_to == "me"
    assert candidate.scheduled_date == date(2026, 5, 20)
    assert candidate.time_window == "lunch"
    assert candidate.metadata["participants"] == "me"
    assert candidate.metadata["needs_exact_time"] == "true"


def test_temporal_update_extracts_restaurant_after_exact_time() -> None:
    update = parse_temporal_update(
        "수요일 점심회식: 오전 11시 30분 편백연가 도룡점",
        reference_date=date(2026, 5, 18),
    )

    assert update["scheduled_date"] == "2026-05-20"
    assert update["time_window"] == "11:30"
    assert update["location"] == "편백연가 도룡점"


def test_temporal_update_treats_after_work_six_as_evening_deadline() -> None:
    update = parse_temporal_update(
        "이 논의는 오늘 퇴근(6시)전에 할 일이야.",
        reference_date=date(2026, 5, 19),
    )

    assert update["due_date"] == "2026-05-19"
    assert update["time_window"] == "18:00"
    assert "location" not in update


def test_temporal_update_understands_next_weekday_feedback() -> None:
    update = parse_temporal_update(
        "차주 복귀해서는 내가 담당자이고, 일시는 월요일 오후 2시경이 좋겠네.",
        reference_date=date(2026, 5, 19),
    )

    assert update["scheduled_date"] == "2026-05-25"
    assert update["time_window"] == "14:00"
    assert "location" not in update


def test_self_intention_without_tags_becomes_this_week_deadline_task() -> None:
    candidates = parse_manual_discussion(
        "이번주 안에 자동차 보험 갱신해야겠다",
        discussion_id="manual/2026-05-18",
        reference_date=date(2026, 5, 18),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.item_type == "task"
    assert candidate.assigned_to == "me"
    assert candidate.due_date == date(2026, 5, 24)
    assert candidate.scheduled_date is None
    assert candidate.title == "자동차 보험 갱신해야겠다"


def test_plain_weekday_range_business_trip_becomes_blocking_range_event() -> None:
    candidates = parse_manual_discussion(
        "수~금 출장이라 수요일 점심회식 못 갈 듯",
        discussion_id="manual/2026-05-18",
        reference_date=date(2026, 5, 18),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.item_type == "event"
    assert candidate.assigned_to == "me"
    assert candidate.scheduled_date == date(2026, 5, 20)
    assert candidate.time_window == "all_day"
    assert candidate.metadata["date_window_start"] == "2026-05-20"
    assert candidate.metadata["date_window_end"] == "2026-05-22"
    assert candidate.metadata["date_window_kind"] == "range"
    assert "needs_exact_date" not in candidate.metadata
    assert candidate.metadata["event_scope"] == "away"
    assert candidate.metadata["blocks_in_person"] == "true"
    assert candidate.metadata["participants"] == "me"
    assert candidate.metadata["location_optional"] == "true"

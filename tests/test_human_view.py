from __future__ import annotations

from datetime import date, datetime

from task_management.domain import Proposal
from task_management.human_view import (
    build_missing_slot_question,
    render_confirmed_sentence,
    render_missing_slot_reminder,
    render_missing_slot_sentence,
)


def test_missing_slot_question_is_channel_neutral_and_human_readable() -> None:
    proposal = Proposal(
        proposal_id="proposal/kea-follow-up",
        source_message_id="slack/DTEST/1000.000001",
        proposer_id="me",
        title="ProjectA 후속 조치",
        raw_text="ProjectA 진행상황도 Follow up 해야함.",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000001",
        required_approvers=("me",),
        missing_slots=("date",),
        created_at=datetime(2026, 5, 18, 9),
        updated_at=datetime(2026, 5, 18, 9),
    )

    question = build_missing_slot_question(proposal, request_id="approval/kea")
    sentence = render_missing_slot_sentence(question)
    reminder = render_missing_slot_reminder(question)

    assert question.title == "ProjectA 후속 조치"
    assert question.assignee_label == "사용자님"
    assert question.missing_slot_labels == ("일시",)
    assert "담당자는 사용자님으로 잡혀 있지만 아직 정해지지 않은 정보가 있습니다: *일시*" in sentence
    assert "*일시* 정보를 알려주세요" in sentence
    assert "`변경 approval/kea ..." in sentence
    assert reminder.startswith("*확인이 필요한 항목입니다.*")
    assert "예: `변경 approval/kea 다음 주 화요일 오전`" in reminder


def test_missing_time_question_mentions_exact_time_and_known_lunch_window() -> None:
    proposal = Proposal(
        proposal_id="proposal/lunch",
        source_message_id="slack/DTEST/1000.000005",
        proposer_id="me",
        title="수요일 점심회식",
        raw_text="수요일 점심회식 참석자 나",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="social",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000005",
        missing_slots=("time", "location"),
        created_at=datetime(2026, 5, 18, 10),
        updated_at=datetime(2026, 5, 18, 10),
        time_window="lunch",
    )

    question = build_missing_slot_question(proposal, request_id="approval/lunch")
    sentence = render_missing_slot_sentence(question)

    assert question.missing_slot_labels == ("정확한 시간", "장소")
    assert "현재 잡힌 일정 후보는 점심 시간대입니다" in sentence
    assert "*정확한 시간*, *장소* 정보를 알려주세요" in sentence


def test_confirmed_sentence_uses_human_actor_label_and_korean_particle() -> None:
    proposal = Proposal(
        proposal_id="proposal/pack-trip",
        source_message_id="slack/DTEST/1000.000009",
        proposer_id="me",
        title="출장 준비 자료 싸기",
        raw_text="내일 아침 출장 전에 오늘 밤 준비 자료 싸기",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="errand",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000009",
        created_at=datetime(2026, 5, 18, 21),
        updated_at=datetime(2026, 5, 18, 21),
    )

    sentence = render_confirmed_sentence(proposal, include_short_id=False)

    assert sentence == "출장 준비 자료 싸기를 진행하면 됩니다. 담당자는 사용자님입니다."
    assert "나가 맡기로 했습니다" not in sentence


def test_confirmed_sentence_does_not_duplicate_until_suffix() -> None:
    proposal = Proposal(
        proposal_id="proposal/midnight",
        source_message_id="slack/DTEST/1000.000010",
        proposer_id="me",
        title="작업 로그 정리",
        raw_text="작업 로그 정리",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000010",
        due_date=date(2026, 5, 20),
        time_window="자정까지",
        created_at=datetime(2026, 5, 18, 21),
        updated_at=datetime(2026, 5, 18, 21),
    )

    sentence = render_confirmed_sentence(proposal, include_short_id=False)

    assert "자정까지까지" not in sentence
    assert sentence.startswith("2026년 5월 20일(수) 자정까지 ")


def test_confirmed_sentence_keeps_deadline_suffix_for_morning_window() -> None:
    proposal = Proposal(
        proposal_id="proposal/morning",
        source_message_id="slack/DTEST/1000.000012",
        proposer_id="me",
        title="보고서 정리",
        raw_text="보고서 정리",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000012",
        due_date=date(2026, 5, 20),
        time_window="morning",
        created_at=datetime(2026, 5, 18, 21),
        updated_at=datetime(2026, 5, 18, 21),
    )

    sentence = render_confirmed_sentence(proposal, include_short_id=False)

    assert sentence.startswith("2026년 5월 20일(수) 오전까지 ")
    assert "오전 보고서" not in sentence


def test_confirmed_sentence_mentions_external_counterpart_label() -> None:
    proposal = Proposal(
        proposal_id="proposal/study-group",
        source_message_id="slack/DTEST/1000.000013",
        proposer_id="me",
        title="제3회 study-group 주제 논의",
        raw_text="제3회 study-group 주제 논의",
        kind="event",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000013",
        scheduled_date=date(2026, 5, 20),
        time_window="오후 3시쯤",
        metadata={
            "participants": "me",
            "participant_label": "박연구 박사님",
            "location": "회의실 A",
        },
        created_at=datetime(2026, 5, 18, 21),
        updated_at=datetime(2026, 5, 18, 21),
    )

    sentence = render_confirmed_sentence(proposal, include_short_id=False)

    assert "관련자는 박연구 박사님입니다" in sentence
    assert "장소는 회의실 A입니다" in sentence


def test_confirmed_sentence_does_not_append_until_to_date_window() -> None:
    proposal = Proposal(
        proposal_id="proposal/window",
        source_message_id="slack/DTEST/1000.000011",
        proposer_id="me",
        title="워크숍 날짜 정하기",
        raw_text="워크숍 날짜 정하기",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000011",
        metadata={
            "date_window_start": "2026-05-20",
            "date_window_end": "2026-05-22",
        },
        created_at=datetime(2026, 5, 18, 21),
        updated_at=datetime(2026, 5, 18, 21),
    )

    sentence = render_confirmed_sentence(proposal, include_short_id=False)

    assert "중까지" not in sentence
    assert sentence.startswith("2026년 5월 20일(수)~2026년 5월 22일(금) 중 ")

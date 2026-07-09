from __future__ import annotations

from datetime import date, datetime

from task_management.cli import main
from task_management.domain import ApprovalRequest, Proposal
from task_management.slack_page import build_slack_monthly_task_page_model, render_slack_monthly_task_page_markdown
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 18, 9, 0, 0)


def _store(tmp_path) -> TeamTaskStore:
    return TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")


def _proposal(
    proposal_id: str,
    title: str,
    *,
    kind: str = "event",
    status: str = "approved",
    assigned_to: str = "me",
    scheduled_date: date | None = date(2026, 5, 18),
    time_window: str = "16:30",
    metadata: dict[str, str] | None = None,
    missing_slots: tuple[str, ...] = (),
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=f"slack/DTEST/{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        assigned_to=assigned_to,
        task_management_area="general",
        discussion_id="slack/DTEST",
        message_id=f"slack/DTEST/{proposal_id}",
        required_approvers=("me",),
        approvals=("me",) if status == "approved" else (),
        missing_slots=missing_slots,
        scheduled_date=scheduled_date,
        time_window=time_window,
        created_at=NOW,
        updated_at=NOW,
        metadata=metadata or {},
    )


def test_slack_monthly_task_page_renders_personal_checklist_sections(tmp_path) -> None:
    store = _store(tmp_path)
    event = _proposal(
        "proposal/committee",
        "리뷰위원회 발표 배석",
        metadata={
            "participants": "me",
            "external_participants": "김센터 센터장님, 이협업 선생님",
            "participant_label": "나/김센터 센터장님/이협업 선생님",
        },
    )
    question = _proposal(
        "proposal/babyfair",
        "워크숍 방문",
        kind="question",
        status="awaiting_approval",
        scheduled_date=None,
        time_window="",
        missing_slots=("exact_date", "participants"),
        metadata={"date_window_start": "2026-05-21", "date_window_end": "2026-05-24"},
    )
    routine = _proposal(
        "proposal/routine",
        "sample-data sync 미팅",
        kind="routine",
        status="awaiting_approval",
        scheduled_date=date(2026, 5, 19),
        time_window="10:00",
        metadata={"location": "3층 회의실", "participants": "me,teammate"},
    )
    prep = _proposal(
        "proposal/prep",
        "sample-data sync 미팅 자료 준비",
        kind="task",
        status="approved",
        scheduled_date=None,
        time_window="",
        metadata={"link_type": "prep_subtask", "parent_proposal_id": "proposal/routine"},
    )
    reference = _proposal(
        "proposal/ref",
        "워크숍 링크",
        kind="reference",
        status="approved",
        scheduled_date=None,
        time_window="",
        metadata={"url": "https://example.com/babyfair"},
    )
    for proposal in (event, question, routine, prep, reference):
        store.save_proposal(proposal)
    store.save_approval_request(
        ApprovalRequest(
            request_id="approval/babyfair",
            proposal_id=question.proposal_id,
            approver_id="me",
            requested_at=NOW,
        )
    )

    model = build_slack_monthly_task_page_model(store, actor_id="me", month=date(2026, 5, 1))
    markdown = render_slack_monthly_task_page_markdown(model)

    assert "# 2026년 5월 개인 Task Management" in markdown
    assert "- [ ] 리뷰위원회 발표 배석 · 2026년 5월 18일(월) 16:30 · 담당 사용자님 · 참석 나/김센터 센터장님/이협업 선생님" in markdown
    assert "## 승인/확인 필요" in markdown
    assert "수락 approval/babyfair" in markdown
    assert "## 떠 있는 항목" in markdown
    assert "확인 필요 정확한 날짜, 참여자" in markdown
    assert "## 반복 루틴" in markdown
    assert "sample-data sync 미팅 · 2026년 5월 19일(화) 10:00 · 담당 사용자님 · 참석 사용자님, 팀원님 · 장소 3층 회의실" in markdown
    assert "## 준비 작업" in markdown
    assert "sample-data sync 미팅 자료 준비" in markdown
    assert "## 참고 링크" in markdown
    assert "https://example.com/babyfair" in markdown


def test_slack_monthly_page_orders_same_day_items_by_clock_minutes(tmp_path) -> None:
    # Regression: the personal surfaces (secretary/monthly-page/digest) used to
    # sort same-day items by the RAW time_window string, so '오후 10시' (22:00)
    # sorted before '오후 2시' (14:00) lexicographically. They now share
    # sort_keys.schedule_first_sort_key, which tie-breaks on real clock minutes.
    store = _store(tmp_path)
    afternoon_late = _proposal("proposal/late", "늦은 오후 회의", time_window="오후 10시")
    afternoon_early = _proposal("proposal/early", "이른 오후 회의", time_window="오후 2시")
    morning = _proposal("proposal/morning", "오전 회의", time_window="오전 9시")
    for proposal in (afternoon_late, afternoon_early, morning):
        store.save_proposal(proposal)

    model = build_slack_monthly_task_page_model(store, actor_id="me", month=date(2026, 5, 1))
    markdown = render_slack_monthly_task_page_markdown(model)

    # Chronological order: 오전 9시 < 오후 2시 < 오후 10시.
    assert markdown.index("오전 회의") < markdown.index("이른 오후 회의") < markdown.index("늦은 오후 회의")
    # And the same in the structured model that drives every personal surface.
    assert [item.proposal_id for item in model.approved] == [
        "proposal/morning",
        "proposal/early",
        "proposal/late",
    ]


def test_slack_monthly_page_excludes_rejected_scheduled_items(tmp_path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/approved", "확정된 오후 회의"))
    store.save_proposal(_proposal("proposal/rejected", "거절한 오후 회의", status="rejected"))

    model = build_slack_monthly_task_page_model(store, actor_id="me", month=date(2026, 5, 1))
    markdown = render_slack_monthly_task_page_markdown(model)

    assert [item.proposal_id for item in model.approved] == ["proposal/approved"]
    assert "확정된 오후 회의" in markdown
    assert "거절한 오후 회의" not in markdown


def test_render_slack_monthly_page_cli_writes_markdown(tmp_path) -> None:
    state = tmp_path / "state"
    store = TeamTaskStore(state / "task_management.sqlite3", state / "events.jsonl")
    store.save_proposal(_proposal("proposal/committee", "리뷰위원회 발표 배석"))
    output = tmp_path / "out" / "slack-page.md"

    main(
        [
            "--state",
            str(state),
            "render-slack-monthly-page",
            "--month",
            "2026-05",
            "--actor",
            "me",
            "--output",
            str(output),
        ]
    )

    markdown = output.read_text(encoding="utf-8")
    assert "2026년 5월 개인 Task Management" in markdown
    assert "리뷰위원회 발표 배석" in markdown

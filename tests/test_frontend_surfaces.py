from __future__ import annotations

from datetime import date, datetime

from task_management.frontend import (
    build_web_task_page_model,
    render_kakao_text_card,
    render_web_task_page_html,
)
from task_management.domain import ApprovalRequest, Proposal
from task_management.slack_home import build_slack_home_view
from task_management.simulator import TeamTaskSimulator


NOW = datetime(2026, 5, 5, 10, 0, 0)


def test_personal_chat_renders_approval_card_and_team_room_gets_outcome(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    created = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/surface-1",
        received_at=NOW,
    )

    approval_message = created.outbound_messages[0]
    assert approval_message.surface == "personal_chat"
    assert approval_message.recipient_id == "teammate"

    card = render_kakao_text_card(approval_message)
    assert "[승인 요청]" in card
    assert "수락 " in card
    assert "거절 " in card
    assert approval_message.approval_request_id in card

    approved = sim.approve(
        approval_message.approval_request_id,
        "teammate",
        decided_at=NOW.replace(hour=10, minute=5),
    )
    team_message = approved.outbound_messages[0]
    assert team_message.surface == "team_room"
    assert team_message.recipient_id == "team"
    assert "[팀 공유]" in render_kakao_text_card(team_message)


def test_web_task_page_model_collects_status_sections(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/web-approved",
        received_at=NOW,
    )
    sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/web-pending",
        received_at=NOW,
    )
    sim.send_private(
        "me",
        "견적서 확인해야 해",
        message_id="dm/me/web-question",
        received_at=NOW,
    )
    sim.send_private(
        "me",
        "https://youtu.be/example",
        message_id="dm/me/web-reference",
        received_at=NOW,
    )

    model = build_web_task_page_model(sim.store, today=date(2026, 5, 5))

    assert model["surface_roles"]["personal_chat"]["label"] == "개인 DM (Slack dogfood)"
    assert model["surface_roles"]["team_room"]["label"] == "팀 공유방 (future)"
    assert model["surface_roles"]["web_task_page"]["label"] == "웹 task page"
    assert model["counts"]["total"] == 4
    assert model["counts"]["approved"] == 2
    assert model["counts"]["awaiting_approval"] == 2
    assert len(model["sections"]["pending_approvals"]) == 2
    assert len(model["sections"]["questions"]) == 1
    assert len(model["sections"]["references"]) == 1
    assert "me" in model["sections"]["by_assignee"]
    assert "teammate" in model["sections"]["by_assignee"]

    html = render_web_task_page_html(model)
    assert "TeamTask Task Page" in html
    assert "DM은 빠른 입력·질문, 웹은 triage·검증, 팀방은 future" in html
    assert "승인 대기" in html
    assert "참고 링크" in html
    assert "Task-core preview" in html
    assert "mutates_files=false" in html
    assert "export 후보" in html
    assert "확인 필요" in html
    assert 'href="#this_week"' in html
    assert 'data-nav-target="pending_approvals"' in html
    assert 'role="button"' in html
    assert 'aria-expanded="false"' in html
    assert "addEventListener(\"click\"" in html


def test_web_task_page_exposes_preview_readiness_and_applied_exports(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    approved = sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/preview-ready",
        received_at=NOW,
    ).proposals[0]
    pending = sim.send_private(
        "me",
        "워크숍 이번주 목~일 중 하루 가야함",
        message_id="dm/me/preview-blocked",
        received_at=NOW,
    ).proposals[0]
    applied = Proposal(
        proposal_id="codex/slack/DTEST/preview-applied/1",
        source_message_id="slack/DTEST/preview-applied",
        proposer_id="me",
        title="ProjectA 후속조치",
        raw_text="내가 이번주 안에 ProjectA 후속조치 해야겠다",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="general",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/preview-applied",
        required_approvers=("me",),
        approvals=("me",),
        due_date=date(2026, 5, 10),
        created_at=NOW,
        updated_at=NOW,
    )
    sim.store.save_proposal(applied)
    sim.store.mark_proposal_applied(
        applied.proposal_id,
        "task_management-preview-001",
        applied_at=NOW.replace(hour=11),
    )

    model = build_web_task_page_model(sim.store, today=date(2026, 5, 5))

    assert model["preview_counts"]["ready"] == 1
    assert model["preview_counts"]["blocked"] == 1
    assert model["preview_counts"]["applied"] == 1
    ready_item = next(item for item in model["sections"]["this_week"] if item["proposal_id"] == approved.proposal_id)
    blocked_item = next(item for item in model["sections"]["questions"] if item["proposal_id"] == pending.proposal_id)
    applied_item = next(item for item in model["sections"]["this_week"] if item["proposal_id"] == applied.proposal_id)
    assert ready_item["preview_status"] == "ready"
    assert ready_item["preview_label"] == "export 후보"
    assert blocked_item["preview_status"] == "blocked"
    assert blocked_item["preview_label"] == "확인 필요"
    assert applied_item["preview_status"] == "applied"
    assert applied_item["export_item_id"] == "task_management-preview-001"

    html = render_web_task_page_html(model)
    assert "Export 후보" in html
    assert "적용 표시" in html
    assert "task_management-preview-001" in html
    assert 'data-preview="ready"' in html
    assert 'data-preview="blocked"' in html
    assert 'data-preview="applied"' in html


def test_date_window_is_visible_in_kakao_card_and_web_model(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    result = sim.send_private(
        "me",
        "워크숍 이번주 목~일 중 하루 가야함",
        message_id="dm/me/surface-date-window",
        received_at=NOW,
    )

    text_card = render_kakao_text_card(result.outbound_messages[0])
    assert "가능 기간" in text_card
    assert "2026년 5월 7일(목)~2026년 5월 10일(일)" in text_card
    assert "정확한 날짜" in text_card
    assert "참여자" in text_card

    model = build_web_task_page_model(sim.store, today=date(2026, 5, 5))
    question = model["sections"]["questions"][0]
    assert question["date_window"] == "이번주 목~일 (2026년 5월 7일(목)~2026년 5월 10일(일))"
    assert question["missing_slots"] == "exact_date, participants"


def test_slack_home_dedupes_pending_request_and_floating_missing_item(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    proposal = Proposal(
        proposal_id="proposal/terms",
        source_message_id="slack/DTEST/terms",
        proposer_id="me",
        title="약관 2차 수정안 검토 및 교육페이지 약관 추가",
        raw_text="약관 확인 필요",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="DTEST",
        message_id="slack/DTEST/terms",
        required_approvers=("me",),
        missing_slots=("exact_date",),
        time_window="14:00",
        created_at=NOW,
        updated_at=NOW,
        metadata={"needs_exact_date": "true", "participants": "me"},
    )
    sim.store.save_proposal(proposal)
    sim.store.save_approval_request(
        ApprovalRequest(
            request_id="approval/terms",
            proposal_id=proposal.proposal_id,
            approver_id="me",
            requested_at=NOW,
        )
    )

    view = build_slack_home_view(sim.store, now=NOW)
    attention_text = next(
        block["text"]["text"]
        for block in view["blocks"]
        if block.get("type") == "section" and block["text"]["text"].startswith("*확인 필요*")
    )

    assert attention_text.count("약관 2차 수정안 검토 및 교육페이지 약관 추가") == 1
    assert "확인: 정확한 날짜" in attention_text


def test_slack_home_today_section_includes_open_overdue_items(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/overdue",
            source_message_id="slack/DTEST/overdue",
            proposer_id="me",
            title="밀린 보고서 정리",
            raw_text="어제까지 보고서 정리",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/overdue",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 4),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/today",
            source_message_id="slack/DTEST/today",
            proposer_id="me",
            title="오늘 약관 검토",
            raw_text="오늘 약관 검토",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/today",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 5),
            time_window="15:00",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/today-early",
            source_message_id="slack/DTEST/today-early",
            proposer_id="me",
            title="오전 우선 확인",
            raw_text="오전 우선 확인",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/today-early",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 5),
            time_window="09:00",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/done-overdue",
            source_message_id="slack/DTEST/done-overdue",
            proposer_id="me",
            title="완료된 어제 항목",
            raw_text="완료된 어제 항목",
            kind="task",
            status="done",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/done-overdue",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 4),
            created_at=NOW,
            updated_at=NOW,
        )
    )

    model = build_web_task_page_model(sim.store, today=NOW.date())
    today_title_list = [item["title"] for item in model["sections"]["today"]]
    today_titles = set(today_title_list)
    overdue_item = next(item for item in model["sections"]["today"] if item["title"] == "밀린 보고서 정리")
    today_item = next(item for item in model["sections"]["today"] if item["title"] == "오늘 약관 검토")
    assert "밀린 보고서 정리" in today_titles
    assert "오늘 약관 검토" in today_titles
    assert today_title_list.index("오전 우선 확인") < today_title_list.index("오늘 약관 검토")
    assert today_title_list[-1] == "밀린 보고서 정리"
    assert "완료된 어제 항목" not in today_titles
    assert overdue_item["is_overdue"] == "true"
    assert overdue_item["urgency_label"] == "마감 지남"
    assert today_item["is_overdue"] == ""

    view = build_slack_home_view(sim.store, now=NOW)
    today_text = next(
        block["text"]["text"]
        for block in view["blocks"]
        if block.get("type") == "section" and block["text"]["text"].startswith("*오늘*")
    )

    assert "밀린 보고서 정리" in today_text
    assert "🔴 밀린 보고서 정리" not in today_text
    assert "🔴 마감 지남" in today_text
    assert "오늘 약관 검토" in today_text
    assert today_text.index("오전 우선 확인") < today_text.index("오늘 약관 검토")
    assert today_text.index("오늘 약관 검토") < today_text.index("밀린 보고서 정리")
    assert "2026년 5월 4일(월)" in today_text
    assert "2026년 5월 5일(화)" in today_text
    assert "완료된 어제 항목" not in today_text

    html = render_web_task_page_html(model)
    assert 'data-overdue="true"' in html
    assert "마감 지남" in html
    assert 'task-row[data-overdue="true"]' not in html


def test_home_and_web_task_page_show_progress_state(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/progress",
            source_message_id="slack/DTEST/progress",
            proposer_id="me",
            title="Terms follow-up",
            raw_text="Terms follow-up",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/progress",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 5),
            time_window="18:00",
            created_at=NOW,
            updated_at=NOW,
            metadata={
                "participants": "me",
                "progress_status": "partial",
                "remaining_work": "final send",
                "progress_note": "Draft sent; final mail remains.",
                "progress_updated_at": "2026-05-05T10:00:00",
            },
        )
    )

    model = build_web_task_page_model(sim.store, today=NOW.date())
    item = model["sections"]["today"][0]
    assert item["progress_status"] == "partial"
    assert item["remaining_work"] == "final send"
    assert item["progress_note"] == "Draft sent; final mail remains."

    html = render_web_task_page_html(model)
    assert "진행 partial" in html
    assert "남은 일 final send" in html
    assert "Draft sent; final mail remains." in html

    view = build_slack_home_view(sim.store, now=NOW)
    today_text = next(
        block["text"]["text"]
        for block in view["blocks"]
        if block.get("type") == "section" and block["text"]["text"].startswith("*오늘*")
    )
    assert "진행: partial" in today_text
    assert "남은 일: final send" in today_text


def test_week_sections_are_sorted_by_deadline(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/later",
            source_message_id="slack/DTEST/later",
            proposer_id="me",
            title="이번 주 늦은 보고",
            raw_text="이번 주 늦은 보고",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/later",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 8),
            time_window="18:00",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    sim.store.save_proposal(
        Proposal(
            proposal_id="proposal/earlier",
            source_message_id="slack/DTEST/earlier",
            proposer_id="me",
            title="내일 오전 확인",
            raw_text="내일 오전 확인",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/earlier",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 6),
            time_window="09:00",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    model = build_web_task_page_model(sim.store, today=NOW.date())
    week_titles = [item["title"] for item in model["sections"]["this_week"]]
    assert week_titles == ["내일 오전 확인", "이번 주 늦은 보고"]

    view = build_slack_home_view(sim.store, now=NOW)
    week_text = next(
        block["text"]["text"]
        for block in view["blocks"]
        if block.get("type") == "section" and block["text"]["text"].startswith("*이번 주*")
    )
    assert week_text.index("내일 오전 확인") < week_text.index("이번 주 늦은 보고")


def test_web_task_page_renders_event_time_and_participants(tmp_path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    sim.store.save_proposal(
        Proposal(
            proposal_id="codex/slack/DTEST/committee/1",
            source_message_id="slack/DTEST/committee",
            proposer_id="me",
            title="리뷰위원회 발표 배석",
            raw_text="내일 오후 4시 반 리뷰위원회 발표 배석. 참석자 나/김센터 센터장님/이협업 선생님",
            kind="event",
            status="approved",
            assigned_to="me",
            task_management_area="general",
            discussion_id="slack/DTEST",
            message_id="slack/DTEST/committee",
            required_approvers=("me",),
            approvals=("me",),
            scheduled_date=date(2026, 5, 18),
            time_window="16:30",
            created_at=NOW,
            updated_at=NOW,
            metadata={
                "participants": "me",
                "external_participants": "김센터 센터장님, 이협업 선생님",
                "participant_label": "나/김센터 센터장님/이협업 선생님",
                "location_optional": "true",
            },
        )
    )

    model = build_web_task_page_model(sim.store, today=date(2026, 5, 17))
    item = model["sections"]["this_week"][0]
    assert item["date_label"] == "2026년 5월 18일(월)"
    assert item["time_window"] == "16:30"
    assert item["participants"] == "나/김센터 센터장님/이협업 선생님"

    html = render_web_task_page_html(model)
    assert "리뷰위원회 발표 배석" in html
    assert "2026년 5월 18일(월)" in html
    assert "시간 16:30" in html
    assert "참석 나/김센터 센터장님/이협업 선생님" in html
    assert "task-detail-this-week-codex-slack-DTEST-committee-1" in html
    assert "Source" in html

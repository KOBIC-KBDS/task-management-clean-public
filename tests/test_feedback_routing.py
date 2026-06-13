from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import pytest

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision, ProposalDraft, ProposalPatch
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 19, 10, 0, 0)


class CreateOneAgent:
    def __init__(self, title: str, *, item_type: str = "task") -> None:
        self.title = title
        self.item_type = item_type
        self.calls = 0

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        self.calls += 1
        return OperatingAgentDecision(
            action="create_proposals",
            source="routing_test",
            confidence=0.99,
            rationale="Unrelated new message should stay on the proposal-creation path.",
            proposal_drafts=(
                ProposalDraft(
                    source_key=f"routing/{message.message_id}/1",
                    raw_text=message.text,
                    title=self.title,
                    discussion_id=f"{message.visibility}/{message.chat_id}/{message.message_id}",
                    message_id=f"{message.message_id}/routing/1",
                    line_number=1,
                    speaker=message.sender_id,
                    assigned_to="me",
                    task_management_area="work",
                    due_date=date(2026, 5, 23),
                    item_type=self.item_type,
                    metadata={"participants": "me"},
                ),
            ),
        )


class CreateAndPatchAgent:
    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        return OperatingAgentDecision(
            action="create_proposals",
            source="routing_test",
            confidence=0.99,
            rationale="One message can both create a new item and update an existing item.",
            proposal_patches=(
                ProposalPatch(
                    request_id="",
                    proposal_id="proposal/terms",
                    actor_id=message.sender_id,
                    body="Terms review is partially done; finish the final send by evening.",
                    temporal_update={
                        "due_date": "2026-05-20",
                        "time_window": "18:00",
                        "progress_status": "partial",
                        "remaining_work": "final send",
                        "semantic_update_type": "progress",
                    },
                    reason="mixed_create_and_progress",
                    target_confidence=0.96,
                    evidence_text="terms partially done",
                ),
            ),
            proposal_drafts=(
                ProposalDraft(
                    source_key=f"routing/{message.message_id}/new",
                    raw_text=message.text,
                    title="New follow-up meeting",
                    discussion_id=f"{message.visibility}/{message.chat_id}/{message.message_id}",
                    message_id=f"{message.message_id}/routing/new",
                    line_number=1,
                    speaker=message.sender_id,
                    assigned_to="me",
                    task_management_area="work",
                    due_date=date(2026, 5, 21),
                    item_type="event",
                    metadata={"participants": "me"},
                ),
            ),
        )


class NoActionSourceAgent:
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls = 0

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        self.calls += 1
        return OperatingAgentDecision(
            action="no_action",
            source=self.source,
            confidence=0.99,
            rationale="Test no_action source for semantic-first fallback policy.",
        )


class MisroutedSinglePendingPatchAgent:
    def __init__(self, request_id: str, proposal_id: str) -> None:
        self.request_id = request_id
        self.proposal_id = proposal_id
        self.calls = 0

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        self.calls += 1
        return OperatingAgentDecision(
            action="apply_feedback",
            source="codex_cli",
            confidence=0.88,
            rationale="Regression fixture: semantic layer tried to resolve unrelated new meeting against the lone pending card.",
            proposal_patches=(
                ProposalPatch(
                    request_id=self.request_id,
                    proposal_id=self.proposal_id,
                    actor_id=message.sender_id,
                    body=message.text,
                    temporal_update={
                        "time_window": "13:30",
                        "location": "회의실",
                        "semantic_update_type": "correction",
                    },
                    reason="resolve_pending_question_partial_slots",
                    target_confidence=0.78,
                    evidence_text=message.text,
                ),
            ),
        )


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def _message(text: str, *, ts: str = "1000.000001") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"slack/DTEST/{ts}",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text=text,
        received_at=NOW,
    )


def _pending(
    title: str,
    proposal_id: str,
    request_id: str,
    *,
    missing_slots: tuple[str, ...] = ("date",),
    scheduled_date: date | None = None,
    time_window: str = "",
    metadata: dict[str, str] | None = None,
) -> Proposal:
    proposal = Proposal(
        proposal_id=proposal_id,
        source_message_id=f"slack/DTEST/source-{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id=f"private/DTEST/source-{proposal_id}",
        message_id=f"slack/DTEST/source-{proposal_id}/1",
        required_approvers=("me",),
        missing_slots=missing_slots,
        scheduled_date=scheduled_date,
        time_window=time_window,
        created_at=NOW,
        updated_at=NOW,
        metadata=metadata or {"participants": "me"},
    )
    return proposal


def _save_pending(store: TeamTaskStore, proposal: Proposal, request_id: str) -> None:
    store.save_proposal(proposal)
    store.save_approval_request(
        ApprovalRequest(
            request_id=request_id,
            proposal_id=proposal.proposal_id,
            approver_id="me",
            requested_at=NOW,
        )
    )


def test_unrelated_keep_both_language_does_not_resolve_pending_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conflict = _pending(
        "출장 준비물 싸기",
        "proposal/trip-pack",
        "approval/trip-pack",
        missing_slots=("conflict_resolution",),
        scheduled_date=date(2026, 5, 19),
        metadata={
            "participants": "me",
            "conflict_detected": "true",
            "conflict_with_proposal_ids": "proposal/existing",
        },
    )
    _save_pending(store, conflict, "approval/trip-pack")
    agent = CreateOneAgent("Example Lab 워크숍 날짜 정하기")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("Example Lab 워크숍 후보 날짜 조사하고 장소 후보 정리해야 함")
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "Example Lab 워크숍 날짜 정하기"
    unchanged = store.get_proposal(conflict.proposal_id)
    assert unchanged is not None
    assert unchanged.status == "awaiting_approval"
    assert unchanged.missing_slots == ("conflict_resolution",)
    assert "conflict_resolution_action" not in unchanged.metadata


def test_mixed_create_decision_applies_existing_progress_patch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        Proposal(
            proposal_id="proposal/terms",
            source_message_id="slack/DTEST/terms",
            proposer_id="me",
            title="Terms review",
            raw_text="Review terms",
            kind="task",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="DTEST",
            message_id="slack/DTEST/terms",
            required_approvers=("me",),
            approvals=("me",),
            due_date=date(2026, 5, 19),
            created_at=NOW,
            updated_at=NOW,
            metadata={"participants": "me"},
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=CreateAndPatchAgent()).handle_message(
        _message("Create one new item and update the terms item.", ts="1000.000011")
    )

    terms = store.get_proposal("proposal/terms")
    assert terms is not None
    assert terms.due_date == date(2026, 5, 20)
    assert terms.time_window == "18:00"
    assert terms.metadata["progress_status"] == "partial"
    assert terms.metadata["remaining_work"] == "final send"
    assert "New follow-up meeting" in {proposal.title for proposal in result.proposals}
    assert "proposal_progress_updated" in {message.message_type for message in result.outbound_messages}


def test_unrelated_new_task_does_not_update_previous_pending_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "[실사용테스트 S05] 회식 참석 충돌 확인",
            "proposal/lunch-conflict",
            "approval/lunch-conflict",
            missing_slots=("time",),
            scheduled_date=date(2026, 5, 20),
            time_window="lunch",
        ),
        "approval/lunch-conflict",
    )
    _save_pending(
        store,
        _pending(
            "[실사용테스트 S11] Example Lab 워크숍 준비물 확인",
            "proposal/workshop",
            "approval/workshop",
            missing_slots=("date",),
        ),
        "approval/workshop",
    )
    agent = CreateOneAgent("sample-data sync 미팅")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("[실사용테스트 S06] sample-data sync 미팅 매주 화요일 오전 10시 3층 회의실", ts="1000.000002")
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "sample-data sync 미팅"
    lunch = store.get_proposal("proposal/lunch-conflict")
    workshop = store.get_proposal("proposal/workshop")
    assert lunch is not None and workshop is not None
    assert lunch.missing_slots == ("time",)
    assert lunch.metadata.get("state_linked_clause", "") == ""
    assert workshop.missing_slots == ("date",)
    assert workshop.metadata.get("state_linked_clause", "") == ""


def test_explicit_title_feedback_still_updates_matching_pending_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "Example Lab 워크숍 준비물 확인",
            "proposal/workshop",
            "approval/workshop",
            missing_slots=("date",),
        ),
        "approval/workshop",
    )
    _save_pending(
        store,
        _pending(
            "회식 참석 충돌 확인",
            "proposal/lunch-conflict",
            "approval/lunch-conflict",
            missing_slots=("time",),
        ),
        "approval/lunch-conflict",
    )

    agent = NoActionSourceAgent("rule_based")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("Example Lab 워크숍는 금요일 오후로 정하자", ts="1000.000003")
    )

    assert agent.calls == 1
    assert {proposal.proposal_id for proposal in result.proposals} == {"proposal/workshop"}
    workshop = store.get_proposal("proposal/workshop")
    lunch = store.get_proposal("proposal/lunch-conflict")
    assert workshop is not None and lunch is not None
    assert workshop.scheduled_date is None
    assert workshop.due_date == date(2026, 5, 22)
    assert workshop.time_window == "afternoon"
    assert workshop.status == "approved"
    assert workshop.missing_slots == ()
    assert lunch.missing_slots == ("time",)


def test_generic_meeting_word_does_not_hijack_pending_kickoff_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kickoff = _pending(
        "프로젝트 킥오프 회의 잡기",
        "proposal/kickoff",
        "approval/kickoff",
        missing_slots=("exact_date",),
        metadata={
            "participants": "me",
            "external_participants": "김담당 선생님, 이담당 선생님",
            "date_window_start": "2026-05-25",
            "date_window_end": "2026-05-31",
        },
    )
    _save_pending(store, kickoff, "approval/kickoff")
    agent = CreateOneAgent("개인 예약 예약", item_type="event")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("수요일 오전 10시에 개인 예약 예약이 있는데 같은 시간 팀 회의가 겹칠 수 있어.", ts="1000.000004")
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "개인 예약 예약"
    unchanged = store.get_proposal("proposal/kickoff")
    assert unchanged is not None
    assert unchanged.missing_slots == ("exact_date",)
    assert unchanged.metadata.get("last_resolution_update_message_id", "") == ""


def test_generic_meeting_word_does_not_hijack_multi_pending_feedback_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "프로젝트 킥오프 회의",
            "proposal/kickoff",
            "approval/kickoff",
            missing_slots=("exact_date",),
            metadata={
                "participants": "me",
                "external_participants": "김담당 선생님, 이담당 선생님",
                "date_window_start": "2026-05-25",
                "date_window_end": "2026-05-31",
            },
        ),
        "approval/kickoff",
    )
    _save_pending(
        store,
        _pending(
            "예산표 정리 알림",
            "proposal/budget-reminder",
            "approval/budget-reminder",
            missing_slots=("time",),
        ),
        "approval/budget-reminder",
    )
    agent = CreateOneAgent("개인 예약 예약", item_type="event")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("수요일 오전 10시에 개인 예약 예약이 있는데 같은 시간 팀 회의가 겹칠 수 있어.", ts="1000.000006")
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "개인 예약 예약"
    kickoff = store.get_proposal("proposal/kickoff")
    reminder = store.get_proposal("proposal/budget-reminder")
    assert kickoff is not None and reminder is not None
    assert kickoff.missing_slots == ("exact_date",)
    assert reminder.missing_slots == ("time",)
    assert kickoff.metadata.get("state_linked_clause", "") == ""


def test_transition_word_does_not_hijack_pending_item_with_matching_title_word(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "제3회 study-group 주제 논의",
            "proposal/study-group",
            "approval/study-group",
            missing_slots=("time",),
            scheduled_date=date(2026, 5, 27),
            time_window="오전",
            metadata={"participants": "me", "participant_label": "박연구 박사님"},
        ),
        "approval/study-group",
    )
    _save_pending(
        store,
        _pending(
            "약관 2차 수정안 검토 및 교육페이지 약관 추가",
            "proposal/terms",
            "approval/terms",
            missing_slots=("exact_date",),
            metadata={
                "participants": "me",
                "external_owner": "전산개발실 송왕호",
                "materials": "서비스 이용약관, 예시 포털 개인정보처리방침, DataPortal 약관, 교육페이지 이용약관",
                "needs_exact_date": "true",
            },
        ),
        "approval/terms",
    )
    agent = CreateOneAgent("DataPortal 고도화 인터뷰 자료 전달")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message(
            "이번주 내로 KRA 고도화 인터뷰 화면설계 자료를 완성해야하는데, "
            "그 안에 sample-data 양식 고도화, sample data QA report 관련 페이지 반영해야함.\n"
            "추가로 DataPortal쪽 내가 김담당 선생님께 고도화 인터뷰 자료 전달드려야함. 내일 퇴근전까지!",
            ts="1000.000007",
        )
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "DataPortal 고도화 인터뷰 자료 전달"
    ai_study = store.get_proposal("proposal/study-group")
    terms = store.get_proposal("proposal/terms")
    assert ai_study is not None and terms is not None
    assert ai_study.missing_slots == ("time",)
    assert terms.missing_slots == ("exact_date",)
    assert terms.metadata.get("state_linked_clause", "") == ""


def test_action_word_overlap_does_not_hijack_single_pending_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "약관 검토 일정 확정",
            "proposal/terms-review",
            "approval/terms-review",
            missing_slots=("exact_date",),
            metadata={"participants": "me", "materials": "서비스 이용약관"},
        ),
        "approval/terms-review",
    )
    agent = CreateOneAgent("논문 검토")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("논문 검토를 내일 퇴근 전까지 해야 함", ts="1000.000008")
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "논문 검토"
    terms = store.get_proposal("proposal/terms-review")
    assert terms is not None
    assert terms.status == "awaiting_approval"
    assert terms.missing_slots == ("exact_date",)
    assert terms.metadata.get("last_resolution_update_message_id", "") == ""


def test_semantic_single_pending_mismatch_falls_back_to_new_meeting_creation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    s8 = _pending(
        "검색 페이지 방향 아이디어 요청",
        "proposal/search-page",
        "approval/search-page",
        missing_slots=("date",),
    )
    _save_pending(store, s8, "approval/search-page")
    agent = MisroutedSinglePendingPatchAgent("approval/search-page", "proposal/search-page")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("1시 반에 회의실에서 외부 연락 후속 회의", ts="1000.000012")
    )

    assert agent.calls == 1
    unchanged = store.get_proposal("proposal/search-page")
    unchanged_request = store.get_approval_request("approval/search-page")
    assert unchanged is not None
    assert unchanged.status == "awaiting_approval"
    assert unchanged.time_window == ""
    assert unchanged.metadata.get("location", "") == ""
    assert unchanged_request is not None
    assert unchanged_request.status == "pending"

    created = [proposal for proposal in result.proposals if proposal.proposal_id != "proposal/search-page"]
    assert len(created) == 1
    meeting = created[0]
    assert meeting.kind == "event"
    assert meeting.status == "approved"
    assert meeting.scheduled_date == date(2026, 5, 19)
    assert meeting.time_window == "13:30"
    assert meeting.metadata["location"] == "회의실"
    assert meeting.metadata["participants"] == "me"
    event_types = [event["type"] for event in store.read_events()]
    assert "agent.patch.rejected" in event_types
    assert "agent.feedback_reinterpreted_as_new_work" in event_types
    assert "proposal.created" in event_types


@pytest.mark.parametrize("source", ["codex_cli", "claude_code_cli", "openai_responses"])
def test_trusted_semantic_no_action_prevents_state_linked_fallback(tmp_path: Path, source: str) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "약관 2차 수정안 검토 및 교육페이지 약관 추가",
            "proposal/terms",
            "approval/terms",
            missing_slots=("exact_date",),
            metadata={
                "participants": "me",
                "materials": "서비스 이용약관, 예시 포털 개인정보처리방침, DataPortal 약관, 교육페이지 이용약관",
                "needs_exact_date": "true",
            },
        ),
        "approval/terms",
    )
    agent = NoActionSourceAgent(source)

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("약관은 금요일 퇴근전까지 검토하면 돼", ts="1000.000010")
    )

    assert agent.calls == 1
    assert result.proposals == ()
    terms = store.get_proposal("proposal/terms")
    assert terms is not None
    assert terms.status == "awaiting_approval"
    assert terms.missing_slots == ("exact_date",)
    assert terms.metadata.get("last_resolution_update_message_id", "") == ""
    event_types = [event["type"] for event in store.read_events()]
    assert "agent.decision.created" in event_types
    assert "slack.message.reconciled" not in event_types


def test_nonsemantic_no_action_allows_state_linked_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "약관 2차 수정안 검토 및 교육페이지 약관 추가",
            "proposal/terms",
            "approval/terms",
            missing_slots=("exact_date",),
            metadata={
                "participants": "me",
                "materials": "서비스 이용약관, 예시 포털 개인정보처리방침, DataPortal 약관, 교육페이지 이용약관",
                "needs_exact_date": "true",
            },
        ),
        "approval/terms",
    )
    agent = NoActionSourceAgent("rule_based")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("약관은 금요일 퇴근전까지 검토하면 돼", ts="1000.000011")
    )

    assert agent.calls == 1
    assert {proposal.proposal_id for proposal in result.proposals} == {"proposal/terms"}
    terms = store.get_proposal("proposal/terms")
    assert terms is not None
    assert terms.status == "approved"
    assert terms.missing_slots == ()
    assert terms.due_date == date(2026, 5, 22)
    assert terms.time_window == ""
    event_types = [event["type"] for event in store.read_events()]
    assert "agent.decision.created" in event_types
    assert "slack.message.reconciled" in event_types


def test_mixed_pending_feedback_and_new_work_falls_through_to_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _pending(
            "제3회 study-group 주제 논의",
            "proposal/study-group",
            "approval/study-group",
            missing_slots=("time",),
            scheduled_date=date(2026, 5, 27),
            metadata={"participants": "me", "participant_label": "박연구 박사님"},
        ),
        "approval/study-group",
    )
    _save_pending(
        store,
        _pending(
            "약관 2차 수정안 검토 및 교육페이지 약관 추가",
            "proposal/terms",
            "approval/terms",
            missing_slots=("exact_date",),
            metadata={
                "participants": "me",
                "external_owner": "전산개발실 송왕호",
                "materials": "서비스 이용약관, 예시 포털 개인정보처리방침, DataPortal 약관, 교육페이지 이용약관",
                "needs_exact_date": "true",
            },
        ),
        "approval/terms",
    )
    agent = CreateOneAgent("혼합 메시지 semantic 처리")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message(
            "약관은 금요일 오후까지 검토하면 돼. "
            "추가로 DataPortal쪽 내가 김담당 선생님께 고도화 인터뷰 자료 전달드려야함. 내일 퇴근전까지!",
            ts="1000.000009",
        )
    )

    assert agent.calls == 1
    assert result.proposals[0].title == "혼합 메시지 semantic 처리"
    terms = store.get_proposal("proposal/terms")
    ai_study = store.get_proposal("proposal/study-group")
    assert terms is not None and ai_study is not None
    assert terms.status == "awaiting_approval"
    assert terms.missing_slots == ("exact_date",)
    assert terms.metadata.get("state_linked_clause", "") == ""
    assert ai_study.missing_slots == ("time",)
    event_types = [event["type"] for event in store.read_events()]
    assert "agent.decision.created" in event_types
    assert "state_linked_update.deferred_to_agent" not in event_types


def test_deictic_kickoff_feedback_still_updates_pending_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kickoff = _pending(
        "프로젝트 킥오프 회의 잡기",
        "proposal/kickoff",
        "approval/kickoff",
        missing_slots=("exact_date", "time"),
        metadata={
            "participants": "me",
            "needs_exact_date": "true",
            "needs_exact_time": "true",
            "date_window_start": "2026-05-25",
            "date_window_end": "2026-05-31",
        },
    )
    _save_pending(store, kickoff, "approval/kickoff")

    agent = NoActionSourceAgent("rule_based")

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("그 킥오프 회의는 다음주 화요일 오후 3시로 하자.", ts="1000.000005")
    )

    assert agent.calls == 1
    assert {proposal.proposal_id for proposal in result.proposals} == {"proposal/kickoff"}
    updated = store.get_proposal("proposal/kickoff")
    assert updated is not None
    assert updated.scheduled_date == date(2026, 5, 26)
    assert updated.time_window == "15:00"
    assert updated.status == "approved"


def test_other_actor_filling_slot_does_not_consume_required_approver_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = Proposal(
        proposal_id="proposal/terms",
        source_message_id="slack/DTEST/source-terms",
        proposer_id="teammate",
        title="약관 2차 수정안 검토 및 교육페이지 약관 추가",
        raw_text="약관 2차 수정안 검토 및 교육페이지 약관 추가",
        kind="question",
        status="awaiting_approval",
        assigned_to="teammate",
        task_management_area="work",
        discussion_id="private/DTEST/source-terms",
        message_id="slack/DTEST/source-terms/1",
        required_approvers=("teammate",),
        missing_slots=("exact_date",),
        created_at=NOW,
        updated_at=NOW,
        metadata={
            "participants": "teammate",
            "materials": "통합이용약관, 통합포털 개인정보처리방침, scDB 약관, 교육페이지 이용약관",
            "needs_exact_date": "true",
        },
    )
    store.save_proposal(proposal)
    store.save_approval_request(
        ApprovalRequest(
            request_id="approval/terms",
            proposal_id="proposal/terms",
            approver_id="teammate",
            requested_at=NOW,
        )
    )
    agent = NoActionSourceAgent("rule_based")

    # Actor 'me' (not the required approver) fills the missing slot via the fallback reconciler.
    TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("약관은 금요일 퇴근전까지 검토하면 돼", ts="1000.000011")
    )

    assert agent.calls == 1
    # The required approver's pending request must NOT be consumed by another actor.
    teammate_request = store.get_approval_request("approval/terms")
    assert teammate_request is not None
    assert teammate_request.status == "pending"
    # The proposal must not be wrongly approved: the required approver still has to decide.
    terms = store.get_proposal("proposal/terms")
    assert terms is not None
    assert terms.status == "awaiting_approval"
    assert "teammate" in terms.required_approvers
    assert "teammate" not in terms.approvals

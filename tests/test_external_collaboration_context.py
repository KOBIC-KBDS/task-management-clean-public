from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision, ProposalDraft, ProposalPatch
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 19, 17, 30, 32)


class DraftAgent:
    def __init__(self, drafts: Sequence[ProposalDraft]) -> None:
        self.drafts = tuple(drafts)

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        return OperatingAgentDecision(
            action="create_proposals",
            source="semantic_test",
            confidence=0.99,
            rationale="External 담당자 work list parsed into child proposals.",
            proposal_drafts=self.drafts,
        )


class PatchAgent:
    def __init__(self, patch: ProposalPatch) -> None:
        self.patch = patch

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        return OperatingAgentDecision(
            action="apply_feedback",
            source="semantic_test",
            confidence=0.93,
            rationale="Matched free-form slot update to an existing approved proposal.",
            proposal_patches=(self.patch,),
        )


class MixedPatchAndDraftAgent:
    def __init__(self, patch: ProposalPatch, drafts: Sequence[ProposalDraft]) -> None:
        self.patch = patch
        self.drafts = tuple(drafts)

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        return OperatingAgentDecision(
            action="apply_feedback",
            source="semantic_test",
            confidence=0.91,
            rationale="A single user message completed an existing task and introduced new tasks.",
            proposal_patches=(self.patch,),
            proposal_drafts=self.drafts,
        )


def test_mixed_feedback_message_applies_patch_and_creates_new_tasks(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    existing = Proposal(
        proposal_id="manual/migration",
        source_message_id="manual/source",
        proposer_id="me",
        title="프로젝트 이관",
        raw_text="프로젝트 이관",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="ops",
        discussion_id="codex-local",
        message_id="manual/source",
        required_approvers=("me",),
        approvals=("me",),
        missing_slots=(),
        due_date=date(2026, 5, 26),
        time_window="16:00",
        created_at=NOW.replace(hour=9),
        updated_at=NOW.replace(hour=9),
        metadata={"participants": "me"},
    )
    store.save_proposal(existing)
    message = IncomingMessage(
        message_id="slack/DTEST/3001.000001",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text="프로젝트는 이관했음. 그리고 내일 펀드해지랑 대출 처리해야함.",
        received_at=NOW,
    )
    agent = MixedPatchAndDraftAgent(
        ProposalPatch(
            request_id="",
            proposal_id=existing.proposal_id,
            actor_id="me",
            body="프로젝트는 이관했음.",
            temporal_update={"status": "done", "semantic_update_type": "completion"},
            reason="completion_feedback_for_migration_task",
            target_confidence=0.9,
            evidence_text="프로젝트는 이관했음",
        ),
        (
            _draft(
                "codex/slack/DTEST/3001.000001/1",
                "펀드 해지",
                "내일 펀드해지해야함",
                {"participants": "me"},
                due_date=date(2026, 5, 20),
                time_window="10:00",
            ),
            _draft(
                "codex/slack/DTEST/3001.000001/2",
                "대출 처리",
                "내일 대출 처리해야함",
                {"participants": "me"},
                due_date=date(2026, 5, 20),
                time_window="10:00",
            ),
        ),
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(message)

    assert {proposal.title for proposal in result.proposals} == {"프로젝트 이관", "펀드 해지", "대출 처리"}
    updated = store.get_proposal(existing.proposal_id)
    assert updated is not None
    assert updated.status == "done"
    fund = store.get_proposal("codex/slack/DTEST/3001.000001/1")
    loan = store.get_proposal("codex/slack/DTEST/3001.000001/2")
    assert fund is not None and loan is not None
    assert fund.due_date == date(2026, 5, 20)
    assert loan.due_date == date(2026, 5, 20)


def test_external_counterpart_tasks_keep_me_as_internal_owner_and_inherit_review_context(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    parent = Proposal(
        proposal_id="task_management/return-review",
        source_message_id="slack/DTEST/1000.000001",
        proposer_id="me",
        title="차주 복귀해서 확인할 일들 정리해두기",
        raw_text="차주 복귀해서 확인할 일들 정리해두기",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/1000.000001",
        message_id="slack/DTEST/1000.000001/2",
        required_approvers=("me",),
        approvals=("me",),
        missing_slots=(),
        due_date=date(2026, 5, 25),
        time_window="14:00",
        created_at=NOW.replace(hour=16),
        updated_at=NOW.replace(hour=16),
        metadata={"participants": "me"},
    )
    store.save_proposal(parent)
    message = IncomingMessage(
        message_id="slack/DTEST/1001.000001",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text=(
            "담당자들과 할일 정리했어.\n"
            "DataPortal 담당자 김담당 선생님: 1. monocle 등 trajectory 분석 알고리즘 설정하기.\n"
            "ProjectA 담당자 이담당 선생님: 1. ProjectA 고도화 인터뷰 초안 제작."
        ),
        received_at=NOW,
    )
    agent = DraftAgent(
        (
            _draft(
                "codex/slack/DTEST/1001.000001/1",
                "trajectory 분석 알고리즘 설정",
                "monocle 등 trajectory 분석 알고리즘 설정하기",
                {"external_participants": "김담당 선생님", "participant_label": "DataPortal 담당자 김담당 선생님"},
            ),
            _draft(
                "codex/slack/DTEST/1001.000001/2",
                "ProjectA 고도화 인터뷰 초안 제작",
                "ProjectA 고도화 인터뷰 초안 제작",
                {"external_participants": "이담당 선생님", "participant_label": "ProjectA 담당자 이담당 선생님"},
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(message)

    assert result.approval_requests == ()
    assert {proposal.title for proposal in result.proposals} == {
        "trajectory 분석 알고리즘 설정",
        "ProjectA 고도화 인터뷰 초안 제작",
    }
    for proposal in result.proposals:
        assert proposal.status == "approved"
        assert proposal.kind == "task"
        assert proposal.assigned_to == "me"
        assert proposal.required_approvers == ("me",)
        assert proposal.approvals == ("me",)
        assert proposal.missing_slots == ()
        assert proposal.due_date == date(2026, 5, 25)
        assert proposal.time_window == "14:00"
        assert proposal.metadata["participants"] == "me"
        assert proposal.metadata["internal_owner"] == "me"
        assert proposal.metadata["parent_proposal_id"] == parent.proposal_id
        assert proposal.metadata["context_inherited_date"] == "true"
        assert proposal.metadata["collaboration_context"] == "external_counterpart"
    by_title = {proposal.title: proposal for proposal in result.proposals}
    assert by_title["trajectory 분석 알고리즘 설정"].metadata["external_owner"] == "김담당 선생님"
    assert by_title["ProjectA 고도화 인터뷰 초안 제작"].metadata["external_owner"] == "이담당 선생님"


def test_scheduled_external_discussion_makes_location_optional_and_future_window_decision_pending(
    tmp_path: Path,
) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    message = IncomingMessage(
        message_id="slack/DTEST/2001.000001",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text=(
            "박연구 박사님하고 다음주 월요일 오후 3시쯤 제 3회 study-group 주제 관련 논의 해보기로함. "
            "그리고 study-group는 예상하기로는 6월 초에 할듯. "
            "해당 일정 논의도 다음주 월요일 박연구박사님과 진행할 것."
        ),
        received_at=NOW,
    )
    agent = DraftAgent(
        (
            ProposalDraft(
                source_key="codex/slack/DTEST/2001.000001/1",
                raw_text=(
                    "박연구 박사님하고 다음주 월요일 오후 3시쯤 제 3회 study-group 주제 관련 논의 해보기로함. "
                    "해당 일정 논의도 다음주 월요일 박연구박사님과 진행할 것."
                ),
                title="제3회 study-group 주제·일정 논의",
                discussion_id="private/DTEST/slack/DTEST/2001.000001",
                message_id="slack/DTEST/2001.000001",
                line_number=1,
                speaker="me",
                assigned_to="me",
                task_management_area="general",
                scheduled_date=date(2026, 5, 25),
                time_window="15:00쯤",
                item_type="event",
                needs_review=True,
                metadata={
                    "external_participants": "박연구 박사님",
                    "participant_label": "나/박연구 박사님",
                    "participants": "me",
                    "needs_exact_time": "false",
                },
            ),
            ProposalDraft(
                source_key="codex/slack/DTEST/2001.000001/2",
                raw_text="study-group는 예상하기로는 6월 초에 할듯.",
                title="제3회 study-group 예정",
                discussion_id="private/DTEST/slack/DTEST/2001.000001",
                message_id="slack/DTEST/2001.000001",
                line_number=2,
                speaker="me",
                assigned_to="me",
                task_management_area="general",
                item_type="event",
                needs_review=True,
                metadata={
                    "date_window_start": "2026-06-01",
                    "date_window_end": "2026-06-10",
                    "needs_exact_date": "true",
                    "needs_exact_time": "true",
                    "participants": "me",
                },
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(message)

    assert result.approval_requests == ()
    by_title = {proposal.title: proposal for proposal in result.proposals}
    discussion = by_title["제3회 study-group 주제·일정 논의"]
    assert discussion.status == "approved"
    assert discussion.kind == "event"
    assert discussion.scheduled_date == date(2026, 5, 25)
    assert discussion.time_window == "15:00쯤"
    assert discussion.missing_slots == ()
    assert discussion.metadata["location_optional"] == "true"
    assert discussion.metadata["location_policy"] == "optional_for_work_discussion"

    future = by_title["제3회 study-group 예정"]
    assert future.status == "approved"
    assert future.kind == "decision"
    assert future.missing_slots == ()
    assert future.metadata["date_window_start"] == "2026-06-01"
    assert future.metadata["date_window_end"] == "2026-06-10"
    assert "needs_exact_date" not in future.metadata
    assert "needs_exact_time" not in future.metadata
    assert future.metadata["date_resolution_policy"] == "decide_in_scheduled_discussion"
    assert future.metadata["decision_pending"] == "true"


def test_confident_location_only_feedback_updates_existing_discussion_without_pending_request(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    proposal = Proposal(
        proposal_id="codex/slack/DTEST/study-group/1",
        source_message_id="slack/DTEST/2001.000001",
        proposer_id="me",
        title="제3회 study-group 주제·일정 논의",
        raw_text="박연구 박사님하고 다음주 월요일 오후 3시쯤 제 3회 study-group 주제 관련 논의",
        kind="event",
        status="approved",
        assigned_to="me",
        task_management_area="general",
        discussion_id="private/DTEST/slack/DTEST/2001.000001",
        message_id="slack/DTEST/2001.000001/1",
        required_approvers=("me",),
        approvals=("me",),
        missing_slots=(),
        scheduled_date=date(2026, 5, 25),
        time_window="15:00쯤",
        created_at=NOW,
        updated_at=NOW,
        metadata={
            "external_participants": "박연구 박사님",
            "participant_label": "나/박연구 박사님",
            "participants": "me",
            "location_optional": "true",
        },
    )
    store.save_proposal(proposal)
    message = IncomingMessage(
        message_id="slack/DTEST/2002.000001",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text="박연구박사님과 회의 장소는 회의실 A이야.",
        received_at=NOW,
    )
    agent = PatchAgent(
        ProposalPatch(
            request_id="",
            proposal_id=proposal.proposal_id,
            actor_id="me",
            body="박연구 박사님과의 회의 장소를 회의실 A로 업데이트",
            temporal_update={
                "location": "회의실 A",
                "location_optional": "false",
                "participants": "me",
                "external_participants": "박연구 박사님",
                "participant_label": "나/박연구 박사님",
                "scheduled_date": "2026-05-25",
                "time_window": "15:00쯤",
            },
            reason="matched participant and meeting context",
            target_confidence=0.93,
            evidence_text=message.text,
            assumptions=("박연구박사님과 회의는 기존 study-group 논의 일정이다.",),
            missing_slots=(),
            needs_clarification=False,
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(message)

    assert result.approval_requests == ()
    assert result.outbound_messages[0].message_type == "semantic_patch_applied"
    updated = store.get_proposal(proposal.proposal_id)
    assert updated is not None
    assert updated.metadata["location"] == "회의실 A"
    assert updated.metadata["location_optional"] == "false"
    assert updated.status == "approved"
    assert updated.missing_slots == ()
    assert updated.scheduled_date == date(2026, 5, 25)
    assert updated.time_window == "15:00쯤"
    assert not any(event["type"] == "agent.patch.rejected" for event in store.read_events())


def _draft(
    source_key: str,
    title: str,
    raw_text: str,
    metadata: dict[str, str],
    *,
    due_date: date | None = None,
    time_window: str = "",
) -> ProposalDraft:
    return ProposalDraft(
        source_key=source_key,
        raw_text=raw_text,
        title=title,
        discussion_id="private/DTEST/slack/DTEST/1001.000001",
        message_id=f"{source_key}/message",
        line_number=1,
        speaker="me",
        assigned_to="unassigned",
        task_management_area="work",
        due_date=due_date,
        time_window=time_window,
        item_type="task",
        metadata=metadata,
    )

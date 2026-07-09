from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision, ProposalPatch
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.semantic_context import (
    build_operating_agent_context,
    recent_conversation_from_events,
)
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 19, 16, 0, 0)


class StaticPatchAgent:
    def __init__(self, patches: Sequence[ProposalPatch]) -> None:
        self.patches = tuple(patches)
        self.seen_pending_requests: tuple[ApprovalRequest, ...] = ()
        self.seen_pending_proposals: tuple[Proposal, ...] = ()
        self.seen_message: IncomingMessage | None = None

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        self.seen_message = message
        self.seen_pending_requests = tuple(pending_approval_requests)
        self.seen_pending_proposals = tuple(pending_proposals)
        return OperatingAgentDecision(
            action="apply_feedback",
            source="semantic_test",
            confidence=0.99,
            rationale="LLM resolved feedback against pending proposal cards.",
            proposal_patches=self.patches,
        )


def test_semantic_context_exposes_pending_cards_and_policy() -> None:
    proposal, request = _pending("출장 전 할일 정리", "p1", "r1")
    fallback = OperatingAgentDecision(action="no_action", source="rule_based", confidence=0.2, rationale="baseline")
    context = build_operating_agent_context(
        _message("이 논의는 오늘 퇴근 전까지야"),
        pending_approval_requests=(request,),
        pending_proposals=(proposal,),
        fallback_decision=fallback,
    )

    assert context["semantic_contract"]["must_resolve_target_before_slots"] is True
    assert context["semantic_contract"]["low_confidence_policy"] == "ask_clarification_do_not_mutate"
    assert context["rules"]["target_policy"].startswith("For feedback, choose target proposal_id")
    assert context["rules"]["allowed_item_types"] == ["task", "event", "routine", "reference", "question", "decision"]
    assert "Do not invent new item_type values" in context["rules"]["item_type_policy"]
    assert context["pending_proposal_cards"][0]["proposal_id"] == "p1"
    assert context["pending_proposal_cards"][0]["pending_request_ids"] == ["r1"]
    assert "출장" in context["pending_proposal_cards"][0]["semantic_handles"]


def test_semantic_context_puts_recent_conversation_outside_current_message() -> None:
    fallback = OperatingAgentDecision(action="no_action", source="rule_based", confidence=0.2, rationale="baseline")
    message = replace(
        _message("승인"),
        recent_conversation=(
            {"role": "assistant", "text": "메일 발송은 별도 승인할까요?"},
            {"role": "user", "text": "승인"},
        ),
    )

    context = build_operating_agent_context(
        message,
        pending_approval_requests=(),
        pending_proposals=(),
        fallback_decision=fallback,
    )

    assert "recent_conversation" not in context["message"]
    assert context["recent_conversation"] == [
        {"role": "assistant", "text": "메일 발송은 별도 승인할까요?"},
        {"role": "user", "text": "승인"},
    ]


def test_semantic_context_exposes_relation_metadata_keys() -> None:
    proposal, request = _pending("handoff workflow", "p2", "r2")
    proposal = replace(
        proposal,
        metadata={
            "parent_proposal_id": "p-parent",
            "depends_on_proposal_ids": "p-blocker",
            "step_index": "2",
            "step_count": "3",
            "workflow_id": "handoff",
            "workflow_title": "handoff migration",
        },
    )
    fallback = OperatingAgentDecision(action="no_action", source="rule_based", confidence=0.2, rationale="baseline")

    context = build_operating_agent_context(
        _message("relation metadata"),
        pending_approval_requests=(request,),
        pending_proposals=(proposal,),
        fallback_decision=fallback,
    )

    metadata = context["pending_proposal_cards"][0]["metadata"]
    assert metadata["parent_proposal_id"] == "p-parent"
    assert metadata["depends_on_proposal_ids"] == "p-blocker"
    assert metadata["step_index"] == "2"
    assert metadata["step_count"] == "3"
    assert metadata["workflow_id"] == "handoff"
    assert "depends_on_proposal_ids:p-blocker" in context["pending_proposal_cards"][0]["semantic_handles"]


def test_orchestrator_feeds_recent_user_and_bot_turns_to_semantic_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, request = _pending("메일 발송 승인", "proposal/send-mail", "approval/send-mail")
    _seed(store, proposal, request)
    store.append_event(
        "message.received",
        {"message": replace(_message("메일 발송 작업 잡아줘"), message_id="dm/me/previous")},
        occurred_at=NOW,
    )
    store.append_event(
        "slack.message.sent",
        {
            "recipient_id": "me",
            "message": {"text": "메일 발송은 별도 승인할까요?"},
        },
        occurred_at=NOW,
    )
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id=request.request_id,
                proposal_id=proposal.proposal_id,
                actor_id="me",
                body="승인",
                temporal_update={"status": "confirmed", "semantic_update_type": "confirmation"},
                reason="recent_conversation_confirmation",
                target_confidence=0.96,
                evidence_text="승인",
            ),
        )
    )

    TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message("승인"))

    assert agent.seen_message is not None
    assert agent.seen_message.recent_conversation[-3:] == (
        {"role": "user", "text": "메일 발송 작업 잡아줘"},
        {"role": "assistant", "text": "메일 발송은 별도 승인할까요?"},
        {"role": "user", "text": "승인"},
    )


def test_recent_conversation_excludes_current_message_and_reads_tail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    previous = replace(_message("메일 발송 작업 잡아줘"), message_id="dm/me/previous")
    store.append_event("message.received", {"message": previous}, occurred_at=NOW)
    store.append_event(
        "slack.message.sent",
        {"recipient_id": "me", "message": {"text": "메일 발송은 별도 승인할까요?"}},
        occurred_at=NOW,
    )
    current = replace(_message("승인"), message_id="dm/me/current")
    # Record the current message exactly as handle_message does before assembly.
    store.append_event("message.received", {"message": current}, occurred_at=NOW)

    # Content is unchanged for a small log: the prior turns plus the current
    # message as the trailing user turn, identical to the legacy reconstruction.
    conversation = recent_conversation_from_events(
        store.read_recent_events(),
        chat_id=current.chat_id,
        sender_id=current.sender_id,
        current_message=current,
    )
    assert conversation == (
        {"role": "user", "text": "메일 발송 작업 잡아줘"},
        {"role": "assistant", "text": "메일 발송은 별도 승인할까요?"},
        {"role": "user", "text": "승인"},
    )

    # The current message contextualizes itself exactly once: its own recorded
    # message.received event is skipped during the scan, and it is appended as
    # the single trailing user turn (no duplication).
    assert [turn for turn in conversation if turn == {"role": "user", "text": "승인"}] == [
        {"role": "user", "text": "승인"}
    ]

    # The tail reader returns the same recent window as the full reader for a
    # small log while only parsing a bounded slice of the file.
    assert store.read_recent_events() == store.read_events()
    assert store.read_recent_events(max_events=2) == store.read_events()[-2:]


def test_orchestrator_applies_multiple_high_confidence_semantic_patches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, first_request = _pending("출장 전 할일 정리", "proposal/trip-prep", "approval/trip-prep")
    second, second_request = _pending("차주 복귀 후 확인할 일 정리", "proposal/return-review", "approval/return-review")
    _seed(store, first, first_request)
    _seed(store, second, second_request)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id=first_request.request_id,
                proposal_id=first.proposal_id,
                actor_id="me",
                body="출장 전 할일 정리는 오늘 퇴근 전까지",
                temporal_update={"due_date": "2026-05-19", "time_window": "18:00"},
                reason="semantic_targeted_feedback",
                target_confidence=0.96,
                evidence_text="출장 전 할일 정리는 오늘 퇴근 전까지",
                assumptions=("퇴근 전을 18:00 마감으로 해석",),
                missing_slots=("date",),
            ),
            ProposalPatch(
                request_id=second_request.request_id,
                proposal_id=second.proposal_id,
                actor_id="me",
                body="복귀 후 확인할 일은 월요일 오후 2시",
                temporal_update={"due_date": "2026-05-25", "time_window": "14:00"},
                reason="semantic_targeted_feedback",
                target_confidence=0.94,
                evidence_text="복귀 후 확인할 일은 월요일 오후 2시",
                missing_slots=("date",),
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("semantic patch envelope")
    )

    assert {proposal.proposal_id for proposal in agent.seen_pending_proposals} == {
        "proposal/trip-prep",
        "proposal/return-review",
    }
    assert {proposal.proposal_id for proposal in result.proposals} == {
        "proposal/trip-prep",
        "proposal/return-review",
    }
    trip_prep = store.get_proposal("proposal/trip-prep")
    return_review = store.get_proposal("proposal/return-review")
    trip_request = store.get_approval_request("approval/trip-prep")
    return_request = store.get_approval_request("approval/return-review")
    assert trip_prep is not None
    assert trip_prep.status == "approved"
    assert trip_prep.due_date is not None
    assert trip_prep.due_date.isoformat() == "2026-05-19"
    assert trip_prep.time_window == "18:00"
    assert return_review is not None
    assert return_review.status == "approved"
    assert return_review.due_date is not None
    assert return_review.due_date.isoformat() == "2026-05-25"
    assert trip_request is not None
    assert trip_request.status == "accepted"
    assert return_request is not None
    assert return_request.status == "accepted"
    events = store.read_events()
    assert [event["type"] for event in events].count("agent.patch.accepted") == 2
    assert [event["type"] for event in events].count("proposal.approved") == 2


def test_orchestrator_applies_semantic_rejection_to_pending_approval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, request = _pending("검색 페이지 방향 아이디어 요청", "proposal/search-page", "approval/search-page")
    _seed(store, proposal, request)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id=request.request_id,
                proposal_id=proposal.proposal_id,
                actor_id="me",
                body="거절",
                temporal_update={"status": "rejected", "semantic_update_type": "approval_rejection"},
                reason="explicit_user_rejection",
                target_confidence=0.99,
                evidence_text="거절",
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message("거절"))

    assert result.proposals[0].status == "rejected"
    assert result.approval_requests[0].status == "rejected"
    rejected = store.get_proposal("proposal/search-page")
    rejected_request = store.get_approval_request("approval/search-page")
    assert rejected is not None
    assert rejected.status == "rejected"
    assert rejected.missing_slots == ()
    assert rejected.required_approvers == ()
    assert rejected_request is not None
    assert rejected_request.status == "rejected"
    events = [event["type"] for event in store.read_events()]
    assert "agent.patch.accepted" in events
    assert "approval.rejected" in events
    assert "proposal.rejected" in events


def test_direct_completion_can_close_missing_date_item(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, _request = _pending("회의 결과 정리", "proposal/no-date", "approval/no-date")
    store.save_proposal(proposal)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id="",
                proposal_id=proposal.proposal_id,
                actor_id="me",
                body="회의 결과 정리 끝났어",
                temporal_update={"status": "done", "semantic_update_type": "completion"},
                reason="explicit_completion",
                target_confidence=0.97,
                evidence_text="회의 결과 정리 끝났어",
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message("회의 결과 정리 끝났어"))

    assert result.proposals[0].status == "done"
    completed = store.get_proposal(proposal.proposal_id)
    assert completed is not None
    assert completed.status == "done"
    assert completed.missing_slots == ()
    assert completed.metadata["completed_by"] == "me"
    events = [event["type"] for event in store.read_events()]
    assert "agent.patch.accepted" in events
    assert "proposal.completed" in events
    assert "agent.patch.rejected" not in events


def test_request_scoped_completion_closes_missing_slot_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, request = _pending("회의 결과 정리", "proposal/no-date-request", "approval/no-date-request")
    _seed(store, proposal, request)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id=request.request_id,
                proposal_id=proposal.proposal_id,
                actor_id="me",
                body="완료했어",
                temporal_update={"status": "done", "semantic_update_type": "completion"},
                reason="explicit_completion",
                target_confidence=0.97,
                evidence_text="완료했어",
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message("완료했어"))

    assert result.proposals[0].status == "done"
    assert result.approval_requests[0].status == "accepted"
    completed = store.get_proposal(proposal.proposal_id)
    decided = store.get_approval_request(request.request_id)
    assert completed is not None
    assert completed.status == "done"
    assert completed.missing_slots == ()
    assert decided is not None
    assert decided.status == "accepted"
    events = [event["type"] for event in store.read_events()]
    assert "approval.accepted" in events
    assert "proposal.completed" in events


def test_orchestrator_rejects_low_confidence_semantic_patch_without_mutating(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, request = _pending("어느 작업인지 헷갈리는 일정", "proposal/ambiguous", "approval/ambiguous")
    _seed(store, proposal, request)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id=request.request_id,
                proposal_id=proposal.proposal_id,
                actor_id="me",
                body="그건 내일 하면 돼",
                temporal_update={"due_date": "2026-05-20"},
                reason="ambiguous_target",
                target_confidence=0.4,
                evidence_text="그건 내일 하면 돼",
                assumptions=("지시어 '그건'의 대상이 불명확함",),
                missing_slots=("target",),
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message("그건 내일 하면 돼"))

    assert result.proposals == ()
    assert result.approval_requests == ()
    assert result.outbound_messages[0].message_type == "agent_patch_rejected"
    assert result.outbound_messages[0].card["reason"] == "low_target_confidence"
    unchanged = store.get_proposal("proposal/ambiguous")
    assert unchanged is not None
    assert unchanged.status == "awaiting_approval"
    assert unchanged.due_date is None
    unchanged_request = store.get_approval_request("approval/ambiguous")
    assert unchanged_request is not None
    assert unchanged_request.status == "pending"
    events = store.read_events()
    assert [event["type"] for event in events].count("agent.patch.rejected") == 1
    assert [event["type"] for event in events].count("proposal.approved") == 0


def _store(tmp_path: Path) -> TeamTaskStore:
    return TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")


def _seed(store: TeamTaskStore, proposal: Proposal, request: ApprovalRequest) -> None:
    store.save_proposal(proposal)
    store.save_approval_request(request)


def _pending(title: str, proposal_id: str, request_id: str) -> tuple[Proposal, ApprovalRequest]:
    proposal = Proposal(
        proposal_id=proposal_id,
        source_message_id=f"dm/me/{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="general",
        discussion_id="private/dm/me",
        message_id=f"{proposal_id}/1",
        required_approvers=("me",),
        missing_slots=("date",),
        created_at=NOW,
        updated_at=NOW,
    )
    request = ApprovalRequest(
        request_id=request_id,
        proposal_id=proposal.proposal_id,
        approver_id="me",
        requested_at=NOW,
    )
    return proposal, request


def _message(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"dm/me/{abs(hash(text))}",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=text,
        received_at=NOW,
    )

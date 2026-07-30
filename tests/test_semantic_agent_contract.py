from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import pytest

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
    assert context["rules"]["target_policy"].startswith("For feedback, choose exact proposal_id")
    assert "relation_action=detach_children" in context["rules"]["workflow_restructure_policy"]
    assert "workflow_restructure" in context["rules"]["semantic_update_types"]
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


def test_semantic_context_uses_compact_global_index_and_bounded_relevant_details() -> None:
    parent, _request = _pending("월말 업무 마무리", "proposal/wrapup", "approval/wrapup")
    parent = replace(parent, status="done", missing_slots=(), metadata={"workflow_role": "parent"})
    kso = replace(
        parent,
        proposal_id="proposal/kso-structure",
        source_message_id="source/kso",
        title="서비스 구조 확인",
        raw_text="서비스 구조 확인",
        message_id="kso/1",
        status="approved",
        metadata={"parent_proposal_id": parent.proposal_id},
    )
    gena = replace(
        kso,
        proposal_id="proposal/gena-form",
        source_message_id="source/gena",
        title="검토 양식 고도화",
        raw_text="검토 양식 고도화",
        message_id="gena/1",
    )
    filler = tuple(
        replace(
            parent,
            proposal_id=f"proposal/filler-{index}",
            source_message_id=f"source/filler-{index}",
            title=f"Unrelated archived item {index}",
            raw_text=f"Unrelated archived item {index}",
            message_id=f"filler/{index}",
            metadata={},
        )
        for index in range(80)
    )
    all_proposals = (*filler, parent, kso, gena)
    fallback = OperatingAgentDecision(action="no_action", source="rule_based", confidence=0.2, rationale="baseline")

    context = build_operating_agent_context(
        _message("월말 업무 마무리는 완료로 두고 서비스 구조 확인과 검토 양식 고도화는 독립시켜줘"),
        pending_approval_requests=(),
        pending_proposals=all_proposals,
        fallback_decision=fallback,
    )

    detailed_ids = {item["proposal_id"] for item in context["pending_proposal_cards"]}
    index_ids = {item["proposal_id"] for item in context["proposal_index"]}
    assert context["proposal_counts"] == {"all": 83, "indexed": 64, "detailed_context": len(detailed_ids)}
    assert len(detailed_ids) <= 16
    assert {parent.proposal_id, kso.proposal_id, gena.proposal_id} <= detailed_ids
    assert {parent.proposal_id, kso.proposal_id, gena.proposal_id} <= index_ids
    assert len(index_ids) == 64


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


def test_workflow_restructure_completes_parent_and_detaches_open_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = Proposal(
        proposal_id="proposal/day-close",
        source_message_id="dm/me/day-close",
        proposer_id="me",
        title="업무 마무리",
        raw_text="업무 마무리",
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/dm/me",
        message_id="day-close/1",
        required_approvers=("me",),
        approvals=("me",),
        due_date=date(2026, 6, 22),
        created_at=NOW,
        updated_at=NOW,
        metadata={
            "workflow_role": "parent",
            "workflow_id": "day-close",
            "workflow_title": "업무 마무리",
            "workflow_group_child_ids": "proposal/kso,proposal/gena",
        },
    )
    child_a = replace(
        parent,
        proposal_id="proposal/kso",
        source_message_id="dm/me/kso",
        title="서비스 구조 확인",
        raw_text="서비스 구조 확인",
        message_id="kso/1",
        metadata={
            "parent_proposal_id": parent.proposal_id,
            "workflow_id": "day-close",
            "workflow_title": parent.title,
            "step_index": "1",
            "step_count": "2",
            "completion_scope": "subtask",
            "progress_status": "complete",
            "progress_note": "상위 작업 기준 완료",
        },
    )
    child_b = replace(
        child_a,
        proposal_id="proposal/gena",
        source_message_id="dm/me/gena",
        title="검토 양식 고도화",
        raw_text="검토 양식 고도화",
        message_id="gena/1",
        metadata={
            "parent_proposal_id": parent.proposal_id,
            "workflow_id": "day-close",
            "workflow_title": parent.title,
            "step_index": "2",
            "step_count": "2",
            "depends_on_proposal_ids": child_a.proposal_id,
        },
    )
    for proposal in (parent, child_a, child_b):
        store.save_proposal(proposal)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id="",
                proposal_id=parent.proposal_id,
                actor_id="me",
                body="상위 업무 마무리는 완료하고 두 하위 작업은 독립 작업으로 분리해줘",
                temporal_update={
                    "status": "done",
                    "semantic_update_type": "workflow_restructure",
                    "relation_action": "detach_children",
                    "child_proposal_ids": f"{child_a.proposal_id},{child_b.proposal_id}",
                },
                reason="complete_old_parent_and_keep_children_open",
                target_confidence=0.98,
                evidence_text="업무 마무리, 서비스 구조 확인, 검토 양식 고도화",
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("상위 업무 마무리는 완료하고 두 하위 작업은 독립 작업으로 분리해줘")
    )

    updated_parent = store.get_proposal(parent.proposal_id)
    updated_a = store.get_proposal(child_a.proposal_id)
    updated_b = store.get_proposal(child_b.proposal_id)
    assert updated_parent is not None and updated_parent.status == "done"
    assert updated_parent.metadata["completion_source"] == "semantic_workflow_restructure"
    assert "workflow_group_child_ids" not in updated_parent.metadata
    assert updated_a is not None and updated_a.status == "approved"
    assert updated_b is not None and updated_b.status == "approved"
    for child in (updated_a, updated_b):
        assert "parent_proposal_id" not in child.metadata
        assert "workflow_id" not in child.metadata
        assert "workflow_title" not in child.metadata
        assert "step_index" not in child.metadata
        assert "step_count" not in child.metadata
        assert "completion_scope" not in child.metadata
        assert "progress_status" not in child.metadata
        assert child.metadata["detached_from_proposal_id"] == parent.proposal_id
    assert updated_b.metadata["depends_on_proposal_ids"] == child_a.proposal_id
    assert result.outbound_messages[0].message_type == "workflow_restructured"


def test_workflow_restructure_preserves_existing_parent_completion_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    completed_at = "2026-07-01T12:51:38"
    parent, _request = _pending("완료된 상위 작업", "proposal/done-parent", "approval/done-parent")
    parent = replace(
        parent,
        status="done",
        missing_slots=(),
        metadata={"workflow_role": "parent", "completed_at": completed_at},
    )
    child = replace(
        parent,
        proposal_id="proposal/open-child",
        source_message_id="dm/me/open-child",
        title="남은 하위 작업",
        message_id="open-child/1",
        status="approved",
        metadata={"parent_proposal_id": parent.proposal_id},
    )
    store.save_proposal(parent)
    store.save_proposal(child)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id="",
                proposal_id=parent.proposal_id,
                actor_id="me",
                body="완료된 상위는 그대로 두고 남은 하위 작업을 독립시켜줘",
                temporal_update={
                    "status": "done",
                    "semantic_update_type": "workflow_restructure",
                    "relation_action": "detach_children",
                    "child_proposal_ids": child.proposal_id,
                },
                reason="detach_from_already_completed_parent",
                target_confidence=0.99,
                evidence_text="완료된 상위 작업과 남은 하위 작업",
            ),
        )
    )

    TeamTaskOrchestrator(store, operating_agent=agent).handle_message(
        _message("완료된 상위는 그대로 두고 남은 하위 작업을 독립시켜줘")
    )

    updated_parent = store.get_proposal(parent.proposal_id)
    assert updated_parent is not None
    assert updated_parent.status == "done"
    assert updated_parent.metadata["completed_at"] == completed_at
    assert "proposal.completed" not in [event["type"] for event in store.read_events()]
    assert [event["type"] for event in store.read_events()].count("workflow.restructured") == 1


def test_workflow_restructure_rejects_non_child_without_partial_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent, _request = _pending("상위 작업", "proposal/parent", "approval/parent")
    other_parent, _other_request = _pending("다른 상위 작업", "proposal/other-parent", "approval/other-parent")
    parent = replace(parent, status="approved", missing_slots=(), approvals=("me",))
    other_parent = replace(other_parent, status="approved", missing_slots=(), approvals=("me",))
    foreign_child = replace(
        parent,
        proposal_id="proposal/foreign-child",
        source_message_id="dm/me/foreign-child",
        title="다른 상위의 하위 작업",
        message_id="foreign-child/1",
        metadata={"parent_proposal_id": other_parent.proposal_id},
    )
    for proposal in (parent, other_parent, foreign_child):
        store.save_proposal(proposal)
    agent = StaticPatchAgent(
        (
            ProposalPatch(
                request_id="",
                proposal_id=parent.proposal_id,
                actor_id="me",
                body="이 하위 작업을 독립시켜줘",
                temporal_update={
                    "semantic_update_type": "workflow_restructure",
                    "relation_action": "detach_children",
                    "child_proposal_ids": foreign_child.proposal_id,
                },
                reason="incorrect_parent_target",
                target_confidence=0.99,
                evidence_text="상위 작업과 다른 상위의 하위 작업",
            ),
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message("이 하위 작업을 독립시켜줘"))

    assert result.proposals == ()
    assert result.outbound_messages[0].card["reason"] == "workflow_child_not_owned_by_target"
    unchanged_parent = store.get_proposal(parent.proposal_id)
    unchanged_child = store.get_proposal(foreign_child.proposal_id)
    assert unchanged_parent is not None and unchanged_parent.status == parent.status
    assert unchanged_child is not None
    assert unchanged_child.metadata["parent_proposal_id"] == other_parent.proposal_id
    assert "workflow.restructured" not in [event["type"] for event in store.read_events()]


def test_workflow_restructure_rejects_historical_approver_without_current_ownership(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent, _request = _pending("내 상위 작업", "proposal/owned-parent", "approval/owned-parent")
    parent = replace(
        parent,
        status="approved",
        missing_slots=(),
        approvals=("former-approver", "me"),
    )
    child = replace(
        parent,
        proposal_id="proposal/owned-child",
        source_message_id="dm/me/owned-child",
        title="내 하위 작업",
        message_id="owned-child/1",
        status="approved",
        metadata={"parent_proposal_id": parent.proposal_id},
    )
    store.save_proposal(parent)
    store.save_proposal(child)
    patch = ProposalPatch(
        request_id="",
        proposal_id=parent.proposal_id,
        actor_id="former-approver",
        body="하위 작업을 독립시켜줘",
        temporal_update={
            "semantic_update_type": "workflow_restructure",
            "relation_action": "detach_children",
            "child_proposal_ids": child.proposal_id,
        },
        reason="historical_approver_attempt",
        target_confidence=0.99,
        evidence_text="내 상위 작업과 내 하위 작업",
    )
    result = TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
        replace(_message("하위 작업을 독립시켜줘"), sender_id="former-approver")
    )

    assert result.proposals == ()
    assert result.outbound_messages[0].card["reason"] == "actor_not_authorized_for_direct_patch"
    unchanged = store.get_proposal(child.proposal_id)
    assert unchanged is not None
    assert unchanged.metadata["parent_proposal_id"] == parent.proposal_id


def test_workflow_restructure_rejects_group_pending_or_nested_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent, _request = _pending("상위 작업", "proposal/safe-parent", "approval/safe-parent")
    parent = replace(parent, status="approved", missing_slots=(), approvals=("me",))
    pending_child = replace(
        parent,
        proposal_id="proposal/pending-child",
        source_message_id="dm/me/pending-child",
        title="승인 대기 하위",
        message_id="pending-child/1",
        status="awaiting_approval",
        metadata={"parent_proposal_id": parent.proposal_id},
    )
    nested_child = replace(
        parent,
        proposal_id="proposal/nested-child",
        source_message_id="dm/me/nested-child",
        title="중첩 하위",
        message_id="nested-child/1",
        metadata={"parent_proposal_id": parent.proposal_id, "workflow_role": "parent"},
    )
    grouped_child = replace(
        parent,
        proposal_id="proposal/grouped-child",
        source_message_id="dm/me/grouped-child",
        title="묶음 하위",
        message_id="grouped-child/1",
        metadata={
            "parent_proposal_id": parent.proposal_id,
            "workflow_group_child_ids": "proposal/hidden-descendant",
        },
    )
    separate_child = replace(
        parent,
        proposal_id="proposal/separate-child",
        source_message_id="dm/me/separate-child",
        title="분리 묶음 하위",
        message_id="separate-child/1",
        metadata={
            "parent_proposal_id": parent.proposal_id,
            "workflow_separate_child_ids": "proposal/hidden-separate-descendant",
        },
    )
    grandchild = replace(
        parent,
        proposal_id="proposal/grandchild",
        source_message_id="dm/me/grandchild",
        title="손자 작업",
        message_id="grandchild/1",
        metadata={"parent_proposal_id": nested_child.proposal_id},
    )
    for proposal in (parent, pending_child, nested_child, grouped_child, separate_child, grandchild):
        store.save_proposal(proposal)

    def run(child_id: str, suffix: str):
        patch = ProposalPatch(
            request_id="",
            proposal_id=parent.proposal_id,
            actor_id="me",
            body="하위 작업을 독립시켜줘",
            temporal_update={
                "semantic_update_type": "workflow_restructure",
                "relation_action": "detach_children",
                "child_proposal_ids": child_id,
            },
            reason="unsafe_child_shape",
            target_confidence=0.99,
            evidence_text="상위 작업과 하위 작업",
        )
        return TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
            replace(_message(f"하위 작업을 독립시켜줘 {suffix}"), message_id=f"dm/me/{suffix}")
        )

    pending_result = run(pending_child.proposal_id, "pending")
    nested_result = run(nested_child.proposal_id, "nested")
    grouped_result = run(grouped_child.proposal_id, "grouped")
    separate_result = run(separate_child.proposal_id, "separate")

    assert pending_result.outbound_messages[0].card["reason"] == "workflow_child_not_restructurable"
    assert nested_result.outbound_messages[0].card["reason"] == "nested_workflow_child_not_detachable"
    assert grouped_result.outbound_messages[0].card["reason"] == "nested_workflow_child_not_detachable"
    assert separate_result.outbound_messages[0].card["reason"] == "nested_workflow_child_not_detachable"
    assert store.get_proposal(pending_child.proposal_id).metadata["parent_proposal_id"] == parent.proposal_id  # type: ignore[union-attr]
    assert store.get_proposal(nested_child.proposal_id).metadata["parent_proposal_id"] == parent.proposal_id  # type: ignore[union-attr]


def test_workflow_restructure_rejects_request_scope_and_unsupported_relation_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent, request = _pending("상위 작업", "proposal/direct-parent", "approval/direct-parent")
    parent = replace(parent, status="approved", missing_slots=(), approvals=("me",))
    child = replace(
        parent,
        proposal_id="proposal/direct-child",
        source_message_id="dm/me/direct-child",
        title="하위 작업",
        message_id="direct-child/1",
        metadata={"parent_proposal_id": parent.proposal_id},
    )
    store.save_proposal(parent)
    store.save_proposal(child)
    store.save_approval_request(request)

    request_patch = ProposalPatch(
        request_id=request.request_id,
        proposal_id=parent.proposal_id,
        actor_id="me",
        body="하위 작업을 독립시켜줘",
        temporal_update={
            "semantic_update_type": "workflow_restructure",
            "relation_action": "detach_children",
            "child_proposal_ids": child.proposal_id,
        },
        reason="request_scoped_restructure",
        target_confidence=0.99,
        evidence_text="상위 작업과 하위 작업",
    )
    unsupported_patch = replace(
        request_patch,
        request_id="",
        temporal_update={
            "semantic_update_type": "workflow_restructure",
            "relation_action": "reparent_children",
            "child_proposal_ids": child.proposal_id,
        },
    )

    request_result = TeamTaskOrchestrator(
        store,
        operating_agent=StaticPatchAgent((request_patch,)),
    ).handle_message(replace(_message("요청 기반 구조변경"), message_id="dm/me/request-restructure"))
    unsupported_result = TeamTaskOrchestrator(
        store,
        operating_agent=StaticPatchAgent((unsupported_patch,)),
    ).handle_message(replace(_message("지원하지 않는 구조변경"), message_id="dm/me/unsupported-restructure"))

    assert request_result.outbound_messages[0].card["reason"] == "workflow_restructure_requires_direct_patch"
    assert unsupported_result.outbound_messages[0].card["reason"] == "invalid_workflow_relation_action"
    assert unsupported_result.outbound_messages[0].message_type == "agent_patch_clarification_needed"
    assert store.get_proposal(child.proposal_id).metadata["parent_proposal_id"] == parent.proposal_id  # type: ignore[union-attr]


def test_workflow_restructure_transaction_failure_does_not_emit_acceptance_or_mutate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    parent, _request = _pending("상위 작업", "proposal/atomic-parent", "approval/atomic-parent")
    parent = replace(parent, status="approved", missing_slots=(), approvals=("me",))
    child = replace(
        parent,
        proposal_id="proposal/atomic-child",
        source_message_id="dm/me/atomic-child",
        title="하위 작업",
        message_id="atomic-child/1",
        metadata={"parent_proposal_id": parent.proposal_id},
    )
    store.save_proposal(parent)
    store.save_proposal(child)
    patch = ProposalPatch(
        request_id="",
        proposal_id=parent.proposal_id,
        actor_id="me",
        body="하위 작업을 독립시켜줘",
        temporal_update={
            "semantic_update_type": "workflow_restructure",
            "relation_action": "detach_children",
            "child_proposal_ids": child.proposal_id,
        },
        reason="atomic_failure_probe",
        target_confidence=0.99,
        evidence_text="상위 작업과 하위 작업",
    )

    def fail_atomic_save(*args, **kwargs):
        raise RuntimeError("injected workflow transaction failure")

    monkeypatch.setattr(store, "save_proposals_with_audit_atomic", fail_atomic_save)

    with pytest.raises(RuntimeError, match="injected workflow transaction failure"):
        TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
            replace(_message("하위 작업을 독립시켜줘"), message_id="dm/me/atomic-failure")
        )

    unchanged_parent = store.get_proposal(parent.proposal_id)
    unchanged_child = store.get_proposal(child.proposal_id)
    assert unchanged_parent is not None and unchanged_parent.status == parent.status
    assert unchanged_child is not None
    assert unchanged_child.metadata["parent_proposal_id"] == parent.proposal_id
    event_types = [event["type"] for event in store.read_events()]
    assert "agent.patch.accepted" not in event_types
    assert "workflow.restructured" not in event_types


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

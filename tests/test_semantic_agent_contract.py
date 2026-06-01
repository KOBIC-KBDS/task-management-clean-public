from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision, ProposalPatch
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.semantic_context import build_operating_agent_context
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 19, 16, 0, 0)


class StaticPatchAgent:
    def __init__(self, patches: Sequence[ProposalPatch]) -> None:
        self.patches = tuple(patches)
        self.seen_pending_requests: tuple[ApprovalRequest, ...] = ()
        self.seen_pending_proposals: tuple[Proposal, ...] = ()

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
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

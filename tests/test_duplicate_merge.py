from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import pytest

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision, ProposalPatch
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 8, 5, 13, 10, 50)


class StaticPatchAgent:
    def __init__(self, patches: Sequence[ProposalPatch]) -> None:
        self.patches = tuple(patches)

    def decide(self, message, *, pending_approval_requests, pending_proposals):
        return OperatingAgentDecision(
            action="apply_feedback",
            source="semantic_test",
            confidence=0.99,
            rationale="The user explicitly merged duplicate schedule items.",
            proposal_patches=self.patches,
        )


def test_explicit_conflict_duplicate_merge_closes_request_and_keeps_one_canonical(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = _proposal(
        "proposal/analysis-training",
        "바이브코딩 오믹스 분석 교육",
        status="approved",
        time_window="",
        metadata={"external_owner": "인실리코젠", "participants": "me"},
    )
    source = _proposal(
        "proposal/internal-training",
        "인실리코젠 내부교육",
        status="awaiting_approval",
        time_window="13:00~",
        metadata={
            "conflict_detected": "true",
            "conflict_with_proposal_ids": target.proposal_id,
            "external_participants": "KOBIC 및 원내(추가)",
            "materials": "내부교육 대상: KOBIC 및 원내(추가)",
            "participants": "me",
        },
        missing_slots=("conflict_resolution",),
        approvals=(),
    )
    request = ApprovalRequest(
        request_id="approval/training-merge",
        proposal_id=source.proposal_id,
        approver_id="me",
        requested_at=NOW,
    )
    store.save_proposal(target)
    store.save_proposal(source)
    store.save_approval_request(request)
    patch = ProposalPatch(
        request_id=request.request_id,
        proposal_id=source.proposal_id,
        actor_id="me",
        body="두 교육은 같은 일정이므로 병합",
        temporal_update={
            "semantic_update_type": "duplicate_merge",
            "merge_target_proposal_id": target.proposal_id,
            "title": "인실리코젠 바이브코딩 오믹스 분석교육",
            "external_participants": "KOBIC, 원내(추가), 인실리코젠",
        },
        reason="explicit_same_event_merge",
        target_confidence=0.96,
        evidence_text="두개 같은겁니다. 병합하자.",
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
        _message("두개 같은겁니다. 병합하자.")
    )

    canonical = store.get_proposal(target.proposal_id)
    duplicate = store.get_proposal(source.proposal_id)
    decided = store.get_approval_request(request.request_id)
    assert canonical is not None
    assert canonical.title == "인실리코젠 바이브코딩 오믹스 분석교육"
    assert canonical.status == "approved"
    assert canonical.scheduled_date == date(2026, 9, 28)
    assert canonical.time_window == "13:00~"
    assert canonical.metadata["external_participants"] == "KOBIC, 원내(추가), 인실리코젠"
    assert canonical.metadata["merged_duplicate_proposal_ids"] == source.proposal_id
    assert duplicate is not None
    assert duplicate.status == "rejected"
    assert duplicate.missing_slots == ()
    assert duplicate.required_approvers == ()
    assert duplicate.approvals == ()
    assert duplicate.metadata["merged_into_proposal_id"] == target.proposal_id
    assert decided is not None and decided.status == "accepted"
    assert result.outbound_messages[0].message_type == "proposal_merged"
    event_types = [event["type"] for event in store.read_events()]
    assert "proposal.merged_duplicate" in event_types
    assert "approval.accepted" in event_types


def test_conflict_duplicate_merge_rejects_unrelated_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    actual_conflict = _proposal("proposal/actual", "실제 충돌 일정", status="approved")
    unrelated = _proposal("proposal/unrelated", "무관한 일정", status="approved")
    source = _proposal(
        "proposal/source",
        "병합 대기 일정",
        status="awaiting_approval",
        metadata={
            "conflict_detected": "true",
            "conflict_with_proposal_ids": actual_conflict.proposal_id,
        },
        missing_slots=("conflict_resolution",),
        approvals=(),
    )
    request = ApprovalRequest("approval/source", source.proposal_id, "me", requested_at=NOW)
    for proposal in (actual_conflict, unrelated, source):
        store.save_proposal(proposal)
    store.save_approval_request(request)
    patch = ProposalPatch(
        request_id=request.request_id,
        proposal_id=source.proposal_id,
        actor_id="me",
        body="무관한 일정으로 병합",
        temporal_update={
            "semantic_update_type": "duplicate_merge",
            "merge_target_proposal_id": unrelated.proposal_id,
        },
        target_confidence=0.99,
        evidence_text="무관한 일정으로 병합",
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
        _message("무관한 일정으로 병합")
    )

    assert result.proposals == ()
    assert result.outbound_messages[0].card["reason"] == "duplicate_merge_target_not_in_conflict"
    assert store.get_proposal(source.proposal_id).status == "awaiting_approval"  # type: ignore[union-attr]
    assert store.get_approval_request(request.request_id).status == "pending"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "legacy_update",
    (
        {
            "semantic_update_type": "confirmation",
            "status": "confirmed",
            "conflict_resolution": "same_event",
            "duplicate_of_proposal_id": "proposal/target",
        },
        {
            "semantic_update_type": "confirmation",
            "status": "confirmed",
            "conflict_resolution": "merge_with_existing",
            "merge_target_proposal_id": "proposal/target",
        },
        {
            "semantic_update_type": "correction",
            "conflict_resolution": "merge",
            "merge_target_proposal_id": "proposal/target",
            "title": "병합된 일정",
        },
    ),
)
def test_observed_legacy_merge_shapes_are_promoted_to_real_merge(
    tmp_path: Path,
    legacy_update: dict[str, str],
) -> None:
    store = _store(tmp_path)
    target = _proposal("proposal/target", "기존 일정", status="approved")
    source = _proposal(
        "proposal/source",
        "중복 일정",
        status="awaiting_approval",
        metadata={
            "conflict_detected": "true",
            "conflict_with_proposal_ids": target.proposal_id,
        },
        missing_slots=("conflict_resolution",),
        approvals=(),
    )
    request = ApprovalRequest("approval/source", source.proposal_id, "me", requested_at=NOW)
    store.save_proposal(target)
    store.save_proposal(source)
    store.save_approval_request(request)
    patch = ProposalPatch(
        request_id=request.request_id,
        proposal_id=source.proposal_id,
        actor_id="me",
        body="같은 일정으로 병합",
        temporal_update=legacy_update,
        target_confidence=0.96,
        evidence_text="같은 일정으로 병합",
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
        _message(f"legacy merge {legacy_update['conflict_resolution']}")
    )

    assert result.outbound_messages[0].message_type == "proposal_merged"
    assert store.get_proposal(source.proposal_id).status == "rejected"  # type: ignore[union-attr]
    assert store.get_approval_request(request.request_id).status == "accepted"  # type: ignore[union-attr]


def test_duplicate_merge_transaction_failure_keeps_request_and_both_items_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    target = _proposal("proposal/target", "기존 일정", status="approved")
    source = _proposal(
        "proposal/source",
        "중복 일정",
        status="awaiting_approval",
        metadata={
            "conflict_detected": "true",
            "conflict_with_proposal_ids": target.proposal_id,
        },
        missing_slots=("conflict_resolution",),
        approvals=(),
    )
    request = ApprovalRequest("approval/source", source.proposal_id, "me", requested_at=NOW)
    store.save_proposal(target)
    store.save_proposal(source)
    store.save_approval_request(request)
    patch = ProposalPatch(
        request_id=request.request_id,
        proposal_id=source.proposal_id,
        actor_id="me",
        body="같은 일정으로 병합",
        temporal_update={
            "semantic_update_type": "duplicate_merge",
            "merge_target_proposal_id": target.proposal_id,
        },
        target_confidence=0.99,
        evidence_text="같은 일정으로 병합",
    )

    def fail_atomic_save(*args, **kwargs):
        raise RuntimeError("injected duplicate merge failure")

    monkeypatch.setattr(store, "save_proposals_and_approval_with_audit_atomic", fail_atomic_save)
    with pytest.raises(RuntimeError, match="injected duplicate merge failure"):
        TeamTaskOrchestrator(store, operating_agent=StaticPatchAgent((patch,))).handle_message(
            _message("같은 일정으로 병합")
        )

    assert store.get_proposal(target.proposal_id) == target
    assert store.get_proposal(source.proposal_id) == source
    assert store.get_approval_request(request.request_id) == request
    assert "agent.patch.accepted" not in [event["type"] for event in store.read_events()]


def _store(tmp_path: Path) -> TeamTaskStore:
    return TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")


def _proposal(
    proposal_id: str,
    title: str,
    *,
    status: str,
    time_window: str = "13:00",
    metadata: dict[str, str] | None = None,
    missing_slots: tuple[str, ...] = (),
    approvals: tuple[str, ...] = ("me",),
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=f"source/{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind="event",
        status=status,
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/dm/me",
        message_id=f"message/{proposal_id}",
        required_approvers=("me",),
        approvals=approvals,
        missing_slots=missing_slots,
        scheduled_date=date(2026, 9, 28),
        time_window=time_window,
        created_at=NOW,
        updated_at=NOW,
        metadata=metadata or {},
    )


def _message(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"dm/me/{abs(hash(text))}",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=text,
        received_at=NOW,
    )

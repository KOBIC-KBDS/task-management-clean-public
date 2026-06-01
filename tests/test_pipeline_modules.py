from __future__ import annotations

from datetime import date, datetime

import pytest

from task_management.approval_policy import apply_assignment_policy, apply_initial_policy
from task_management.domain import TeamTaskTaskCandidate, IncomingMessage, Proposal
from task_management.proposal_builder import proposal_from_candidate
from task_management.slot_validator import missing_slots_for_candidate, missing_slots_for_proposal


NOW = datetime(2026, 5, 18, 9, 0, 0)


def _message() -> IncomingMessage:
    return IncomingMessage(
        message_id="slack/DTEST/1000.000001",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text="워크숍 이번주 목~일 중 하루 가야함",
        received_at=NOW,
    )


def test_proposal_builder_converts_candidate_to_canonical_proposal() -> None:
    candidate = TeamTaskTaskCandidate(
        source_key="candidate/babyfair",
        raw_text="워크숍 이번주 목~일 중 하루 가야함",
        title="워크숍 방문",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000001/1",
        line_number=1,
        assigned_to="shared",
        item_type="event",
        metadata={
            "date_window_start": "2026-05-21",
            "date_window_end": "2026-05-24",
            "needs_exact_date": "true",
        },
    )

    proposal = proposal_from_candidate(candidate, message=_message())

    assert proposal.proposal_id == "candidate/babyfair"
    assert proposal.kind == "event"
    assert proposal.status == "draft"
    assert proposal.assigned_to == "shared"
    assert proposal.missing_slots == ("exact_date", "participants", "location")
    assert proposal.metadata["source_provider"] == "slack"
    assert proposal.metadata["source_channel"] == "DTEST"
    assert proposal.metadata["source_ts"] == "1000.000001"
    assert proposal.metadata["parser_assigned_to"] == "shared"
    assert proposal.metadata["source_text_hash"].startswith("sha256:")


def test_proposal_builder_fails_closed_for_unsupported_agent_enums() -> None:
    candidate = TeamTaskTaskCandidate(
        source_key="candidate/unsupported-type",
        raw_text="store this as a memo",
        title="Store memo",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000007/1",
        line_number=1,
        assigned_to="me",
        item_type="memo",
        scheduled_date=date(2026, 5, 20),
    )

    with pytest.raises(ValueError, match="unsupported item_type"):
        proposal_from_candidate(candidate, message=_message())

    bad_assignee = TeamTaskTaskCandidate(
        source_key="candidate/unsupported-assignee",
        raw_text="send vendor a note",
        title="Send vendor note",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000008/1",
        line_number=1,
        assigned_to="vendor",
        item_type="task",
    )

    with pytest.raises(ValueError, match="unsupported assigned_to"):
        proposal_from_candidate(bad_assignee, message=_message())


def test_proposal_builder_preserves_explicit_question_kind() -> None:
    candidate = TeamTaskTaskCandidate(
        source_key="candidate/question",
        raw_text="ask which operating policy to use",
        title="Choose operating policy",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000009/1",
        line_number=1,
        assigned_to="me",
        item_type="question",
        scheduled_date=date(2026, 5, 20),
    )

    proposal = proposal_from_candidate(candidate, message=_message())

    assert proposal.kind == "question"


def test_slot_validator_is_reusable_for_candidates_and_persisted_proposals() -> None:
    candidate = TeamTaskTaskCandidate(
        source_key="candidate/routine",
        raw_text="sample-data sync 미팅 주 1회",
        title="sample-data sync 미팅",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000002/1",
        line_number=1,
        assigned_to="me",
        item_type="routine",
        metadata={"recurrence_frequency": "weekly"},
    )

    assert missing_slots_for_candidate(candidate, assigned_to="me") == ("time", "location", "participants")

    proposal = Proposal(
        proposal_id="proposal/routine",
        source_message_id="slack/DTEST/1000.000002",
        proposer_id="me",
        title="sample-data sync 미팅",
        raw_text="sample-data sync 미팅 주 1회",
        kind="routine",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000002/1",
        metadata={"recurrence_frequency": "weekly", "participants": "me"},
    )

    assert missing_slots_for_proposal(proposal) == ("time", "location")


def test_slot_validator_requires_exact_time_for_lunch_events() -> None:
    candidate = TeamTaskTaskCandidate(
        source_key="candidate/lunch",
        raw_text="수요일 점심회식 참석자 나",
        title="수요일 점심회식",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000005/1",
        line_number=1,
        assigned_to="me",
        item_type="event",
        scheduled_date=date(2026, 5, 20),
        time_window="lunch",
        metadata={"participants": "me", "needs_exact_time": "true"},
    )

    proposal = proposal_from_candidate(candidate, message=_message())

    assert missing_slots_for_candidate(candidate, assigned_to="me") == ("time", "location")
    assert proposal.missing_slots == ("time", "location")

    resolved = Proposal(
        proposal_id="proposal/lunch",
        source_message_id="slack/DTEST/1000.000005",
        proposer_id="me",
        title="수요일 점심회식",
        raw_text="수요일 점심회식 참석자 나",
        kind="event",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="social",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000005/1",
        scheduled_date=date(2026, 5, 20),
        time_window="12:30",
        metadata={"participants": "me", "needs_exact_time": "true", "location": "OO식당"},
    )

    assert missing_slots_for_proposal(resolved) == ()


def test_slot_validator_treats_unknown_time_placeholders_as_missing() -> None:
    candidate = TeamTaskTaskCandidate(
        source_key="candidate/unknown-time",
        raw_text="오늘 회의 시간 미정",
        title="오늘 회의",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000006/1",
        line_number=1,
        assigned_to="me",
        item_type="event",
        scheduled_date=date(2026, 5, 19),
        time_window="?",
        metadata={"participants": "me", "location_optional": "true"},
    )

    proposal = proposal_from_candidate(candidate, message=_message())

    assert missing_slots_for_candidate(candidate, assigned_to="me") == ("time",)
    assert proposal.kind == "event"
    assert proposal.status == "draft"
    assert proposal.missing_slots == ("time",)


def test_approval_policy_returns_state_changes_without_store_access() -> None:
    proposal = Proposal(
        proposal_id="proposal/self",
        source_message_id="slack/DTEST/1000.000003",
        proposer_id="me",
        title="보고서 확인",
        raw_text="내가 내일 보고서 확인할게",
        kind="task",
        status="draft",
        assigned_to="me",
        task_management_area="health",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000003/1",
        due_date=date(2026, 5, 19),
    )

    updated, requests, outbound = apply_initial_policy(proposal, now=NOW)

    assert updated.status == "approved"
    assert updated.required_approvers == ("me",)
    assert updated.approvals == ("me",)
    assert requests == ()
    assert outbound[0].surface == "team_room"


def test_assignment_policy_routes_shared_items_to_missing_approvers() -> None:
    proposal = Proposal(
        proposal_id="proposal/shared",
        source_message_id="slack/DTEST/1000.000004",
        proposer_id="me",
        title="워크숍 방문",
        raw_text="워크숍 토요일 같이 가기",
        kind="event",
        status="draft",
        assigned_to="shared",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1000.000004/1",
        scheduled_date=date(2026, 5, 23),
        metadata={"participants": "me,teammate", "location": "워크숍"},
    )

    updated, requests, outbound = apply_assignment_policy(proposal, actor_id="me", now=NOW)

    assert updated.status == "awaiting_approval"
    assert updated.required_approvers == ("me", "teammate")
    assert updated.approvals == ("me",)
    assert [request.approver_id for request in requests] == ["teammate"]
    assert outbound[0].recipient_id == "teammate"

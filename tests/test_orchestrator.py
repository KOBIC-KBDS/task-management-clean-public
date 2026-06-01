from __future__ import annotations

from datetime import datetime
from pathlib import Path

from task_management.simulator import TeamTaskSimulator
from task_management.task_core_bridge import build_task_management_task_export_from_proposals, validate_with_task_core


NOW = datetime(2026, 5, 5, 10, 0, 0)


def test_utf8_korean_keywords_are_preserved_in_parser_source() -> None:
    source = Path("task_management/discussion_adapter.py").read_text(encoding="utf-8")

    for keyword in ("회의", "고객 미팅", "당신", "내가", "다음주"):
        assert keyword in source
    assert "\ufffd" not in source


def test_private_self_assigned_message_auto_approves_and_notifies_team(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    result = sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/1",
        received_at=NOW,
    )

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.assigned_to == "me"
    assert proposal.status == "approved"
    assert proposal.approvals == ("me",)
    assert proposal.task_management_area == "work"
    assert any(message.channel == "team" for message in result.outbound_messages)
    team_message = next(message for message in result.outbound_messages if message.channel == "team")
    assert "나가 맡기로 했습니다" not in team_message.text
    assert "담당자는 사용자님입니다" in team_message.text


def test_requested_assignee_gets_private_approval_then_task_core_preview(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    created = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/2",
        received_at=NOW,
    )

    assert len(created.proposals) == 1
    proposal = created.proposals[0]
    assert proposal.assigned_to == "teammate"
    assert proposal.status == "awaiting_approval"
    assert proposal.required_approvers == ("teammate",)
    assert len(created.approval_requests) == 1
    request = created.approval_requests[0]
    assert request.approver_id == "teammate"
    assert created.outbound_messages[0].recipient_id == "teammate"

    approved = sim.approve(request.request_id, "teammate", decided_at=NOW.replace(hour=10, minute=5))
    approved_proposal = approved.proposals[0]
    assert approved_proposal.status == "approved"
    assert approved_proposal.approvals == ("teammate",)
    assert any(message.channel == "team" for message in approved.outbound_messages)

    payload = build_task_management_task_export_from_proposals(
        sim.store.list_proposals(),
        exported_at=NOW.replace(hour=10, minute=10),
    )
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["metadata"]["proposal_id"] == proposal.proposal_id
    assert item["metadata"]["proposer_id"] == "me"
    assert item["metadata"]["assigned_to"] == "teammate"
    assert item["metadata"]["required_approvers"] == "teammate"
    assert item["metadata"]["approvals"] == "teammate"
    assert item["metadata"]["source_message_id"] == "dm/me/2"

    preview = validate_with_task_core(payload, root=tmp_path / "task-core-root")
    assert preview["ok"] is True
    assert preview["restores"] is False


def test_shared_task_requires_remaining_person_approval(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    created = sim.send_team(
        "me",
        "우리 같이 이번 주 토요일 장보기 하자",
        message_id="team/1",
        received_at=NOW,
    )

    proposal = created.proposals[0]
    assert proposal.assigned_to == "shared"
    assert proposal.status == "awaiting_approval"
    assert proposal.required_approvers == ("me", "teammate")
    assert proposal.approvals == ("me",)
    assert len(created.approval_requests) == 1
    assert created.approval_requests[0].approver_id == "teammate"

    approved = sim.approve(created.approval_requests[0].request_id, "teammate", decided_at=NOW.replace(hour=11))
    assert approved.proposals[0].status == "approved"
    assert set(approved.proposals[0].approvals) == {"me", "teammate"}


def test_date_window_creates_exact_date_question_for_shared_plan(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    created = sim.send_private(
        "me",
        "워크숍 이번주 목~일 중 하루 가야함",
        message_id="dm/me/babyfair",
        received_at=NOW,
    )

    proposal = created.proposals[0]
    assert proposal.kind == "question"
    assert proposal.assigned_to == "shared"
    assert proposal.status == "awaiting_approval"
    assert proposal.metadata["date_window_start"] == "2026-05-07"
    assert proposal.metadata["date_window_end"] == "2026-05-10"
    assert proposal.missing_slots == ("exact_date", "participants")
    assert created.approval_requests[0].approver_id == "me"
    assert "date_window" in created.outbound_messages[0].card


def test_reference_and_ambiguous_messages_take_safe_paths(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    reference = sim.send_private(
        "me",
        "https://youtu.be/example",
        message_id="dm/me/ref",
        received_at=NOW,
    )
    assert reference.proposals[0].kind == "reference"
    assert reference.proposals[0].status == "approved"
    assert reference.approval_requests == ()

    ambiguous = sim.send_private(
        "me",
        "견적서 확인해야 해",
        message_id="dm/me/ambiguous",
        received_at=NOW,
    )
    assert ambiguous.proposals[0].kind == "question"
    assert ambiguous.proposals[0].status == "awaiting_approval"
    assert set(ambiguous.proposals[0].missing_slots) == {"assigned_to", "date"}
    assert ambiguous.approval_requests[0].approver_id == "me"


def test_store_survives_restart_logs_events_and_ignores_duplicate_message(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    first = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/stable",
        received_at=NOW,
    )
    duplicate = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/stable",
        received_at=NOW,
    )

    restarted = TeamTaskSimulator(tmp_path)
    proposals = restarted.store.list_proposals()
    events = restarted.store.read_events()

    assert len(first.proposals) == 1
    assert duplicate.ignored_duplicate is True
    assert len(proposals) == 1
    assert [event["type"] for event in events] == [
        "message.received",
        "agent.decision.created",
        "proposal.created",
        "approval.requested",
    ]


def test_only_approved_proposals_are_exported_to_task_core_payload(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    approved = sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/approved",
        received_at=NOW,
    )
    pending = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/pending",
        received_at=NOW,
    )

    payload = build_task_management_task_export_from_proposals(sim.store.list_proposals(), exported_at=NOW)

    assert approved.proposals[0].status == "approved"
    assert pending.proposals[0].status == "awaiting_approval"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["metadata"]["proposal_id"] == approved.proposals[0].proposal_id
    assert payload["diagnostics"]["mutates_files"] is False
    assert payload["diagnostics"]["approved_proposal_count"] == 1

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from task_management.backfill_report import build_workflow_backfill_report, write_workflow_backfill_report
from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.frontend import build_web_task_page_model, render_web_task_page_html
from task_management.operating_agent import OperatingAgentDecision, ProposalDraft
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.relations import (
    blocking_dependencies,
    child_proposals,
    relation_sort_key,
    validate_workflow_relations,
    workflow_projection,
)
from task_management.slack_home import build_slack_home_view
from task_management.store import TeamTaskStore
from task_management.task_core_bridge import build_task_management_task_export_from_proposals
from task_management.timeline import proposal_timeline


NOW = datetime(2026, 5, 29, 9, 0, 0)


class StaticDraftAgent:
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
            source="hierarchy_test",
            confidence=0.96,
            rationale="A sequential workflow was detected.",
            proposal_drafts=self.drafts,
        )


def test_relation_projection_orders_by_step_and_surfaces_blockers() -> None:
    parent = _proposal("proposal/workflow", "Workflow")
    first = _proposal(
        "proposal/first",
        "First child",
        due_date=date(2026, 6, 2),
        metadata={"parent_proposal_id": parent.proposal_id, "step_index": "1", "step_count": "2"},
    )
    second = _proposal(
        "proposal/second",
        "Second child",
        due_date=date(2026, 5, 30),
        metadata={
            "parent_proposal_id": parent.proposal_id,
            "step_index": "2",
            "step_count": "2",
            "depends_on_proposal_ids": first.proposal_id,
        },
    )

    assert child_proposals(parent, [parent, second, first]) == (first, second)
    assert relation_sort_key(first) < relation_sort_key(second)
    blockers = blocking_dependencies(second, {first.proposal_id: first, second.proposal_id: second})
    assert blockers == (first,)
    projection = workflow_projection(parent, [parent, first, second])
    assert projection.current_next_step == first
    assert projection.rollup_status == "in_progress"

    errors = validate_workflow_relations(
        (
            first,
            replace(second, proposal_id="proposal/dupe", metadata={**second.metadata, "step_index": "1"}),
        ),
        existing_proposals=(parent,),
    )
    assert [error.code for error in errors] == ["duplicate_step_index"]

    risk_errors = validate_workflow_relations(
        (
            replace(
                second,
                metadata={
                    **second.metadata,
                    "step_index": "2",
                    "risk_level": "high",
                    "risk_reason": "external_send",
                },
            ),
        ),
        existing_proposals=(parent, first),
    )
    assert [error.code for error in risk_errors] == ["risky_child_requires_separate_approval"]


def test_workflow_batch_uses_group_approval_and_keeps_risky_child_separate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = _draft(
        "proposal/workflow",
        "Review launch package",
        metadata={"workflow_role": "parent", "workflow_id": "launch", "workflow_title": "Launch workflow"},
    )
    normal_child = _draft(
        "proposal/check",
        "Check terms",
        due_date=date(2026, 5, 29),
        metadata={
            "parent_proposal_id": "proposal/workflow",
            "workflow_id": "launch",
            "step_index": "1",
            "step_count": "2",
        },
    )
    risky_child = _draft(
        "proposal/send",
        "Send external notice",
        due_date=date(2026, 5, 29),
        metadata={
            "parent_proposal_id": "proposal/workflow",
            "workflow_id": "launch",
            "step_index": "2",
            "step_count": "2",
            "depends_on_proposal_ids": "proposal/check",
            "risk_level": "high",
            "risk_reason": "external_send",
            "requires_separate_approval": "true",
        },
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((parent, normal_child, risky_child))).handle_message(
        _message("sequential workflow")
    )

    stored = {proposal.proposal_id: proposal for proposal in store.list_proposals()}
    assert set(stored) == {"proposal/workflow", "proposal/check", "proposal/send"}
    assert stored["proposal/workflow"].status == "awaiting_approval"
    assert stored["proposal/check"].status == "awaiting_approval"
    assert stored["proposal/send"].status == "awaiting_approval"
    assert len(result.approval_requests) == 2
    group_request = next(request for request in result.approval_requests if request.proposal_id == "proposal/workflow")
    risky_request = next(request for request in result.approval_requests if request.proposal_id == "proposal/send")
    assert group_request.request_id != risky_request.request_id
    assert result.outbound_messages[0].message_type == "workflow_group_approval_request"
    assert result.outbound_messages[0].card["workflow_group_child_ids"] == "proposal/check"
    assert result.outbound_messages[0].card["workflow_separate_child_ids"] == "proposal/send"
    assert "workflow.group_approval.requested" in [event["type"] for event in store.read_events()]

    accepted = TeamTaskOrchestrator(store).handle_approval(
        request_id=group_request.request_id,
        approver_id="me",
        accepted=True,
        decided_at=NOW.replace(hour=10),
    )
    assert {proposal.proposal_id for proposal in accepted.proposals} == {"proposal/workflow", "proposal/check"}
    assert store.get_proposal("proposal/workflow").status == "approved"  # type: ignore[union-attr]
    assert store.get_proposal("proposal/check").status == "approved"  # type: ignore[union-attr]
    assert store.get_proposal("proposal/send").status == "awaiting_approval"  # type: ignore[union-attr]


def test_invalid_workflow_batch_saves_no_proposals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = _draft("proposal/workflow", "Workflow", metadata={"workflow_role": "parent"})
    first = _draft("proposal/first", "First", metadata={"parent_proposal_id": "proposal/workflow", "step_index": "1"})
    duplicate = _draft("proposal/duplicate", "Duplicate", metadata={"parent_proposal_id": "proposal/workflow", "step_index": "1"})

    result = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((parent, first, duplicate))).handle_message(
        _message("invalid workflow")
    )

    assert result.proposals == ()
    assert store.list_proposals() == ()
    assert result.outbound_messages[0].message_type == "workflow_batch_rejected"
    assert "duplicate_step_index" in result.outbound_messages[0].card["error_codes"]
    assert "workflow.batch.rejected" in [event["type"] for event in store.read_events()]


def test_existing_parent_workflow_child_does_not_auto_approve(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/existing", "Existing workflow", status="approved"))
    child = _draft(
        "proposal/new-child",
        "Send external follow-up",
        due_date=date(2026, 5, 29),
        metadata={
            "parent_proposal_id": "proposal/existing",
            "step_index": "2",
            "risk_level": "high",
            "risk_reason": "external_send",
            "requires_separate_approval": "true",
        },
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((child,))).handle_message(
        _message("existing workflow child")
    )

    stored = store.get_proposal("proposal/new-child")
    assert stored is not None
    assert stored.status == "awaiting_approval"
    assert stored.metadata["workflow_group_id"] == "workflow-group/proposal/existing"
    assert len(result.approval_requests) == 1
    assert result.outbound_messages[0].message_type == "approval_request"
    events = [event["type"] for event in store.read_events()]
    assert "proposal.approved" not in events


def test_group_feedback_accepts_parent_and_normal_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = _draft("proposal/workflow", "Workflow", metadata={"workflow_role": "parent"})
    child = _draft(
        "proposal/child",
        "Child",
        due_date=date(2026, 5, 29),
        metadata={"parent_proposal_id": "proposal/workflow", "step_index": "1"},
    )
    created = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((parent, child))).handle_message(
        _message("workflow feedback accept")
    )
    group_request = created.approval_requests[0]

    result = TeamTaskOrchestrator(store).handle_feedback(
        request_id=group_request.request_id,
        actor_id="me",
        body="ok",
        changed_at=NOW.replace(hour=10),
        temporal_update={"semantic_update_type": "confirmation"},
    )

    assert {proposal.proposal_id for proposal in result.proposals} == {"proposal/workflow", "proposal/child"}
    assert store.get_proposal("proposal/workflow").status == "approved"  # type: ignore[union-attr]
    assert store.get_proposal("proposal/child").status == "approved"  # type: ignore[union-attr]
    assert "workflow.group_approval.accepted" in [event["type"] for event in store.read_events()]


def test_task_core_export_skips_workflow_parent_by_default() -> None:
    parent = replace(
        _proposal("proposal/workflow", "Workflow", status="approved"),
        metadata={"workflow_group_child_ids": "proposal/first,proposal/second"},
    )
    first = _proposal(
        "proposal/first",
        "First child",
        status="approved",
        metadata={"parent_proposal_id": parent.proposal_id, "workflow_id": "wf", "step_index": "1"},
    )
    payload = build_task_management_task_export_from_proposals((parent, first), exported_at=NOW)

    assert [item["metadata"]["proposal_id"] for item in payload["items"]] == ["proposal/first"]
    assert payload["items"][0]["metadata"]["workflow_id"] == "wf"
    assert payload["diagnostics"]["workflow_parent_skipped_count"] == 1
    assert payload["diagnostics"]["workflow_parent_skipped_ids"] == ["proposal/workflow"]


def test_home_and_dashboard_group_children_and_show_timeline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = _proposal(
        "proposal/workflow",
        "Workflow",
        due_date=date(2026, 5, 29),
        metadata={"workflow_role": "parent", "workflow_title": "Launch workflow"},
    )
    child = _proposal(
        "proposal/child",
        "Child step",
        due_date=date(2026, 5, 29),
        metadata={"parent_proposal_id": parent.proposal_id, "step_index": "1", "step_count": "1"},
    )
    store.save_proposal(parent)
    store.save_proposal(child)
    store.append_event("proposal.created", {"proposal": parent}, occurred_at=NOW)
    store.append_event("proposal.created", {"proposal": child}, occurred_at=NOW.replace(minute=1))

    model = build_web_task_page_model(store, today=NOW.date())
    today = model["sections"]["today"]
    assert [item["title"] for item in today] == ["Workflow"]
    assert today[0]["children"][0]["title"] == "Child step"
    assert today[0]["rollup_status"] == "in_progress"
    assert today[0]["timeline"][0]["proposal_title"] == "Workflow"
    assert today[0]["timeline"][1]["proposal_title"] == "Child step"

    html = render_web_task_page_html(model)
    assert "task-children" in html
    assert "task-children-label" in html
    assert "하위작업 0/1 완료" in html
    assert "Child step" in html
    assert "Launch workflow" in html

    view = build_slack_home_view(store, now=NOW)
    today_text = next(
        block["text"]["text"]
        for block in view["blocks"]
        if block.get("type") == "section" and block["text"]["text"].startswith("*오늘*")
    )
    assert "Workflow" in today_text
    assert "↳ _하위작업 0/1 완료 · 다음: 1/1 Child step_" in today_text
    assert "└─ [1/1] *Child step*" in today_text


def test_done_dashboard_keeps_workflow_children_under_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = _proposal(
        "proposal/done-workflow",
        "Done workflow",
        status="done",
        due_date=date(2026, 5, 29),
        metadata={"workflow_role": "parent", "workflow_title": "Done workflow"},
    )
    child = _proposal(
        "proposal/done-child",
        "Done child",
        status="done",
        due_date=date(2026, 5, 29),
        metadata={"parent_proposal_id": parent.proposal_id, "step_index": "1", "step_count": "1"},
    )
    store.save_proposal(parent)
    store.save_proposal(child)

    model = build_web_task_page_model(store, today=NOW.date())

    done = model["sections"]["done"]
    assert model["sections"]["today"] == []
    assert model["sections"]["this_week"] == []
    assert [item["title"] for item in done] == ["Done workflow"]
    assert done[0]["children"][0]["title"] == "Done child"
    html = render_web_task_page_html(model)
    assert "하위작업 1/1 완료" in html


def test_timeline_and_backfill_report_are_read_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _proposal("proposal/first", "First", source_message_id="slack/D/1")
    second = _proposal("proposal/second", "Second", source_message_id="slack/D/1")
    store.save_proposal(first)
    store.save_proposal(second)
    store.append_event("proposal.created", {"proposal": first}, occurred_at=NOW)
    before_proposals = store.list_proposals()
    before_events_text = store.event_log_path.read_text(encoding="utf-8")

    timeline = proposal_timeline(store.read_events(), store.list_proposals(), proposal_id=first.proposal_id)
    assert [entry.event_type for entry in timeline] == ["proposal.created"]

    report = build_workflow_backfill_report(store.list_proposals(), store.read_events(), generated_at=NOW)
    output = write_workflow_backfill_report(report, tmp_path / "out" / "report.json")

    assert output.exists()
    assert report["mutation_policy"] == "preview_only_no_store_writes_no_event_append_no_outbound"
    assert report["counts"]["suggestions_generated"] == 1
    assert store.list_proposals() == before_proposals
    assert store.event_log_path.read_text(encoding="utf-8") == before_events_text


def _store(tmp_path: Path) -> TeamTaskStore:
    return TeamTaskStore(tmp_path / "state.sqlite3", tmp_path / "events.jsonl")


def _message(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"slack/DTEST/{abs(hash(text))}",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text=text,
        received_at=NOW,
    )


def _draft(
    source_key: str,
    title: str,
    *,
    due_date: date | None = None,
    metadata: dict[str, str] | None = None,
) -> ProposalDraft:
    return ProposalDraft(
        source_key=source_key,
        raw_text=title,
        title=title,
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/1",
        line_number=1,
        speaker="me",
        assigned_to="me",
        task_management_area="work",
        due_date=due_date,
        item_type="task",
        metadata=metadata or {},
    )


def _proposal(
    proposal_id: str,
    title: str,
    *,
    status: str = "approved",
    due_date: date | None = None,
    source_message_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=source_message_id or f"slack/DTEST/{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind="task",
        status=status,
        assigned_to="me",
        task_management_area="work",
        discussion_id="slack/DTEST",
        message_id=f"slack/DTEST/{proposal_id}",
        required_approvers=("me",),
        approvals=("me",) if status == "approved" else (),
        due_date=due_date,
        created_at=NOW,
        updated_at=NOW,
        metadata=metadata or {},
    )

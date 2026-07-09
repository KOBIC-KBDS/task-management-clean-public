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
    order_child_proposals,
    relation_sort_key,
    requires_separate_approval,
    step_label,
    validate_workflow_relations,
    workflow_projection,
)
from task_management.sort_keys import proposal_deadline_sort_key, time_sort_minutes
from task_management.slack_home import build_slack_home_view
from task_management.secretary import build_morning_briefing
from task_management.store import TeamTaskStore
from task_management.task_core_bridge import build_task_management_task_export_from_proposals
from task_management.timeline import proposal_timeline
from task_management.workflow_normalizer import (
    _series_title,
    normalize_existing_proposal_graph,
    normalize_new_proposal_graph,
)


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


def test_workflow_child_time_tiebreak_matches_deadline_sorter() -> None:
    """Regression: workflow-child time tie-breaks now use the canonical clock parser.

    relations._time_sort_minutes used to be a cruder divergent re-implementation:
    it mapped any "오후..." window to a flat 13*60 (ignoring the actual hour) and a
    bare "오전" to 540 (sort_keys uses 480), and could not read the minute of a
    Korean clock string at all. The deadline sorter (sort_keys.time_sort_minutes)
    instead reads "오후 3시" as 15*60=900. That divergence let workflow-child
    ordering disagree with deadline ordering. The fix routes the relations
    tie-break through sort_keys.time_sort_minutes, so the two now agree.
    """

    parent = _proposal("proposal/wf-time", "Time workflow")
    # Same date, no step_index -> ordering falls to the deadline tie-break, where
    # only the resolved minute (then title) decides order.
    clock = replace(
        _proposal(
            "proposal/clock",
            "힣 13:30 항목",  # title sorts AFTER the 오후 item, so only the minute can reorder it
            due_date=date(2026, 6, 2),
            metadata={"parent_proposal_id": parent.proposal_id},
        ),
        time_window="13:30",
    )
    afternoon = replace(
        _proposal(
            "proposal/afternoon",
            "오후 3시 항목",
            due_date=date(2026, 6, 2),
            metadata={"parent_proposal_id": parent.proposal_id},
        ),
        time_window="오후 3시",
    )

    # The relations tie-break now resolves the canonical clock minutes the deadline
    # sorter uses: "오후 3시" -> 900 (was a flat 780), "13:30" -> 810 (was 780).
    assert time_sort_minutes("오후 3시") == 15 * 60
    assert time_sort_minutes("13:30") == 13 * 60 + 30
    assert time_sort_minutes("오전") == 8 * 60

    # Under the old flat-780 mapping these two tied on minute, so the title
    # decided and "오후 3시 항목" (오 < 힣) sorted first -> (afternoon, clock).
    # With the canonical parser 13:30 (810) precedes 오후 3시 (900), flipping the
    # order to (clock, afternoon) and matching the deadline sorter.
    ordered = order_child_proposals([afternoon, clock])
    assert ordered == (clock, afternoon)

    deadline_ordered = sorted(
        [afternoon, clock],
        key=lambda proposal: proposal_deadline_sort_key(proposal, today=date(2026, 6, 1)),
    )
    assert tuple(deadline_ordered) == ordered


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


def test_workflow_risk_is_read_from_semantic_metadata_not_title_tokens() -> None:
    assert requires_separate_approval(
        _proposal("p/semantic", "외부 공지", metadata={"requires_separate_approval": "true"})
    )
    assert requires_separate_approval(
        _proposal("p/risk-level", "후속 안내", metadata={"risk_level": "high", "risk_reason": "external_send"})
    )
    assert not requires_separate_approval(_proposal("p/title-only", "정보실 메일 안내"))


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


def test_workflow_parent_cycle_is_rejected_without_corrupting_state(tmp_path: Path) -> None:
    # Bug #16: a 2-draft batch with mutual parent_proposal_id (A<->B) must be
    # rejected through the workflow.batch.rejected path, never silently accepted.
    store = _store(tmp_path)
    draft_a = _draft("proposal/a", "Cycle A", metadata={"parent_proposal_id": "proposal/b"})
    draft_b = _draft("proposal/b", "Cycle B", metadata={"parent_proposal_id": "proposal/a"})

    result = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((draft_a, draft_b))).handle_message(
        _message("mutual parent cycle")
    )

    assert result.proposals == ()
    assert store.list_proposals() == ()
    assert result.outbound_messages[0].message_type == "workflow_batch_rejected"
    assert "relation_cycle" in result.outbound_messages[0].card["error_codes"]
    assert "workflow.batch.rejected" in [event["type"] for event in store.read_events()]


def test_resending_identical_commitment_merges_into_existing_canonical(tmp_path: Path) -> None:
    # Bug #17: re-sending an identical note after a prior merge must dedup at
    # intake (same source_text_hash + title/date/time) instead of stacking a
    # second live card.
    store = _store(tmp_path)

    def _commitment_draft(source_key: str) -> ProposalDraft:
        return ProposalDraft(
            source_key=source_key,
            raw_text="3회 보고서 작성",
            title="보고서 작성",
            discussion_id="slack/DTEST",
            message_id=f"slack/DTEST/{source_key}",
            line_number=1,
            speaker="me",
            assigned_to="me",
            task_management_area="work",
            due_date=date(2026, 6, 5),
            time_window="10:00",
            item_type="task",
            metadata={},
        )

    first = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((_commitment_draft("proposal/first"),))).handle_message(
        IncomingMessage(
            message_id="slack/DTEST/first",
            sender_id="me",
            chat_id="DTEST",
            visibility="private",
            text="보고서 작성",
            received_at=datetime(2026, 6, 4, 9, 0),
        )
    )
    canonical = store.get_proposal("proposal/first")
    assert canonical is not None
    assert canonical.status != "rejected"
    first_hash = canonical.metadata["source_text_hash"]

    TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((_commitment_draft("proposal/second"),))).handle_message(
        IncomingMessage(
            message_id="slack/DTEST/second",
            sender_id="me",
            chat_id="DTEST",
            visibility="private",
            text="보고서 작성",
            received_at=datetime(2026, 6, 4, 10, 0),
        )
    )

    duplicate = store.get_proposal("proposal/second")
    assert duplicate is not None
    assert duplicate.metadata["source_text_hash"] == first_hash
    assert duplicate.status == "rejected"
    assert duplicate.metadata["merged_into_proposal_id"] == "proposal/first"
    assert duplicate.required_approvers == ()
    assert duplicate.approvals == ()
    assert "proposal.merged_duplicate" in [event["type"] for event in store.read_events()]

    model = build_web_task_page_model(store, today=date(2026, 6, 5))
    today_cards = [item for item in model["sections"]["today"] if item["title"] == "보고서 작성"]
    assert len(today_cards) == 1
    assert today_cards[0]["proposal_id"] == "proposal/first"
    assert today_cards[0]["status"] != "rejected"


def test_step_labels_recompute_after_third_child_attaches(tmp_path: Path) -> None:
    # Bug #18: existing children must not keep a stale /2 denominator once a
    # third follow-up child attaches under the same root.
    store = _store(tmp_path)
    parent = _proposal(
        "proposal/root",
        "Workflow",
        metadata={"workflow_role": "parent", "workflow_title": "Workflow"},
    )
    first = _proposal(
        "proposal/c1",
        "First",
        due_date=date(2026, 6, 1),
        metadata={"parent_proposal_id": parent.proposal_id, "step_index": "1", "step_count": "2"},
    )
    second = _proposal(
        "proposal/c2",
        "Second",
        due_date=date(2026, 6, 2),
        metadata={"parent_proposal_id": parent.proposal_id, "step_index": "2", "step_count": "2"},
    )

    # Stored labels are stale (1/2, 2/2) until the third child arrives.
    assert step_label(first, (parent, first, second)) == "1/2"

    third = _proposal(
        "proposal/c3",
        "Third",
        due_date=date(2026, 6, 3),
        metadata={"parent_proposal_id": parent.proposal_id, "step_index": "3", "step_count": "2"},
    )
    proposals = (parent, first, second, third)

    assert step_label(first, proposals) == "1/3"
    assert step_label(second, proposals) == "2/3"
    assert step_label(third, proposals) == "3/3"

    for proposal in proposals:
        store.save_proposal(proposal)
        store.append_event("proposal.created", {"proposal": proposal}, occurred_at=NOW)

    model = build_web_task_page_model(store, today=date(2026, 6, 1))
    root_item = next(item for section in model["sections"].values() for item in section if item["title"] == "Workflow")
    child_labels = [child["step_label"] for child in root_item["children"]]
    assert child_labels == ["1/3", "2/3", "3/3"]
    assert "2/2" not in child_labels


def test_series_title_ignores_korean_counting_phrases() -> None:
    # Bug (LOW): counting phrases like '지난 3회 동안 ...' must not become a
    # series identity, while genuine ordinal series stay intact.
    assert _series_title("지난 3회 동안 못 끝낸 보고서") == ""
    assert _series_title("3회째") == ""
    assert _series_title("5회 연속 지각") == ""
    assert _series_title("제3회 ai-study") == "제3회 ai-study"
    assert _series_title("3회 스터디") == "제3회 스터디"

    # An unrelated counting-phrase follow-up task must not be reparented under a
    # real workflow root just because both mention "N회".
    root = replace(
        _proposal("proposal/series-root", "ai-study 발표자료 리뷰 논의", status="approved"),
        kind="event",
        scheduled_date=date(2026, 5, 28),
        metadata={"workflow_role": "parent", "workflow_title": "제3회 ai-study", "workflow_container": "true"},
    )
    unrelated = _proposal(
        "proposal/counting-followup",
        "지난 3회 동안 못 끝낸 보고서 완료보고서 메일 발송",
        status="approved",
        due_date=date(2026, 6, 3),
    )

    normalized = normalize_new_proposal_graph((root,), (unrelated,), normalized_at=datetime(2026, 6, 3, 12, 0))
    reparented = normalized.proposals[0]
    assert reparented.metadata.get("parent_proposal_id", "") != root.proposal_id


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


def test_new_followup_rehomes_under_promoted_workflow_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = replace(
        _proposal("proposal/review", "ai-study 발표자료 리뷰 논의", status="approved"),
        kind="event",
        scheduled_date=date(2026, 5, 28),
        time_window="10:00",
        metadata={"participants": "me", "progress_status": "scheduled_for_13_00_to_13_30"},
    )
    decision_child = replace(
        _proposal("proposal/date-decision", "제3회 ai-study 일정 결정", status="done"),
        kind="event",
        scheduled_date=date(2026, 6, 2),
        time_window="14:00",
        metadata={
            "parent_proposal_id": root.proposal_id,
            "decision_pending": "true",
            "completed_at": "2026-06-02T15:00:00",
        },
    )
    store.save_proposal(root)
    store.save_proposal(decision_child)
    followup = _draft(
        "proposal/followup",
        "3회 ai-study 후속자료·완료보고서 메일 발송",
        due_date=date(2026, 6, 3),
        metadata={
            "parent_proposal_id": decision_child.proposal_id,
            "risk_level": "medium",
            "risk_reason": "external_send",
            "requires_separate_approval": "true",
        },
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((followup,))).handle_message(
        IncomingMessage(
            message_id="slack/DTEST/followup",
            sender_id="me",
            chat_id="DTEST",
            visibility="private",
            text="followup",
            received_at=datetime(2026, 6, 3, 9, 0, 0),
        )
    )

    promoted = store.get_proposal(root.proposal_id)
    created = store.get_proposal("proposal/followup")
    assert promoted is not None
    assert created is not None
    assert promoted.title == "제3회 ai-study"
    assert promoted.metadata["workflow_role"] == "parent"
    assert promoted.metadata["workflow_container"] == "true"
    assert promoted.metadata["previous_title"] == "ai-study 발표자료 리뷰 논의"
    assert created.metadata["parent_proposal_id"] == root.proposal_id
    assert created.metadata["depends_on_proposal_ids"] == decision_child.proposal_id
    assert created.status == "awaiting_approval"
    assert result.approval_requests[0].proposal_id == created.proposal_id
    assert "workflow.graph_normalized" in [event["type"] for event in store.read_events()]

    model = build_web_task_page_model(store, today=date(2026, 6, 3))
    today = model["sections"]["today"]
    assert [item["title"] for item in today] == ["제3회 ai-study"]
    assert today[0]["urgency_label"] == ""
    assert today[0]["children"][0]["title"] == "제3회 ai-study 일정 결정"
    assert today[0]["children"][1]["title"] == "3회 ai-study 후속자료·완료보고서 메일 발송"

    briefing = build_morning_briefing(store, now=datetime(2026, 6, 3, 9, 30), actor_id="me", reserve=False)[0]
    assert "- 제3회 ai-study — 하위작업 중심으로 확인합니다." in briefing.text
    assert "3회 ai-study 후속자료·완료보고서 메일 발송" in briefing.text
    assert "일정 지남 · 결과 확인 필요" not in briefing.text


def test_existing_backfill_rehomes_followups_without_weak_cross_workflow_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = replace(
        _proposal("proposal/review", "ai-study 발표자료 리뷰 논의", status="approved"),
        kind="event",
        scheduled_date=date(2026, 5, 28),
        time_window="10:00",
        metadata={"progress_status": "scheduled_for_13_00_to_13_30"},
    )
    decision_child = replace(
        _proposal("proposal/date-decision", "제3회 ai-study 일정 결정", status="done"),
        kind="event",
        scheduled_date=date(2026, 6, 2),
        time_window="14:00",
        metadata={
            "parent_proposal_id": root.proposal_id,
            "completed_at": "2026-06-02T15:00:00",
        },
    )
    followup = _proposal(
        "proposal/followup",
        "3회 ai-study 후속자료·완료보고서 메일 발송",
        status="awaiting_approval",
        due_date=date(2026, 6, 3),
        metadata={
            "parent_proposal_id": decision_child.proposal_id,
            "workflow_group_id": f"workflow-group/{decision_child.proposal_id}",
            "requires_separate_approval": "true",
        },
    )
    generic_root = _proposal(
        "proposal/generic-root",
        "hierarchy 기능 검증용 문서 정리",
        status="done",
        due_date=date(2026, 6, 1),
        metadata={"workflow_role": "parent", "workflow_title": "hierarchy 기능 검증용 문서 정리"},
    )
    date_like_child = _proposal(
        "proposal/date-like",
        "260526 회의결과 공유",
        status="done",
        due_date=date(2026, 6, 1),
        metadata={"parent_proposal_id": generic_root.proposal_id},
    )
    unrelated_followup = _proposal(
        "proposal/unrelated",
        "KEA 담당자 후속 논의 안건 정리",
        status="approved",
        due_date=date(2026, 6, 4),
    )
    for proposal in (root, decision_child, followup, generic_root, date_like_child, unrelated_followup):
        store.save_proposal(proposal)

    normalized = normalize_existing_proposal_graph(store.list_proposals(), normalized_at=datetime(2026, 6, 3, 12, 0))
    updates = {proposal.proposal_id: proposal for proposal in normalized.updated_existing}

    promoted = updates[root.proposal_id]
    assert promoted.title == "제3회 ai-study"
    assert promoted.metadata["workflow_container"] == "true"
    assert promoted.metadata["workflow_original_title"] == "ai-study 발표자료 리뷰 논의"

    rehomed = updates[followup.proposal_id]
    assert rehomed.metadata["parent_proposal_id"] == root.proposal_id
    assert rehomed.metadata["depends_on_proposal_ids"] == decision_child.proposal_id
    assert rehomed.metadata["workflow_group_id"] == f"workflow-group/{root.proposal_id}"

    assert updates.get(generic_root.proposal_id, generic_root).title == "hierarchy 기능 검증용 문서 정리"
    assert updates[date_like_child.proposal_id].metadata["workflow_title"] == "hierarchy 기능 검증용 문서 정리"
    assert unrelated_followup.proposal_id not in updates


def test_existing_backfill_merges_same_title_task_event_commitment(tmp_path: Path) -> None:
    task = _proposal(
        "proposal/next-week-progress",
        "차주 expression_db 회의·exDB/KEA 미팅",
        status="approved",
        due_date=date(2026, 6, 5),
    )
    task = replace(task, time_window="10:00")
    event = replace(
        _proposal(
            "proposal/next-week-meeting",
            "차주 expression_db 회의·exDB/KEA 미팅",
            status="approved",
        ),
        kind="event",
        due_date=None,
        scheduled_date=date(2026, 6, 5),
        time_window="10:00",
    )

    normalized = normalize_existing_proposal_graph((task, event), normalized_at=datetime(2026, 6, 8, 9, 0))
    updates = {proposal.proposal_id: proposal for proposal in normalized.updated_existing}

    canonical = updates[event.proposal_id]
    duplicate = updates[task.proposal_id]
    assert canonical.status == "approved"
    assert canonical.metadata["merged_duplicate_proposal_ids"] == task.proposal_id
    assert duplicate.status == "rejected"
    assert duplicate.metadata["merged_into_proposal_id"] == event.proposal_id
    assert duplicate.metadata["merged_original_status"] == "approved"

    store = _store(tmp_path)
    store.save_proposal(canonical)
    store.save_proposal(duplicate)
    model = build_web_task_page_model(store, today=date(2026, 6, 8))
    today_titles = [item["title"] for item in model["sections"]["today"]]
    assert today_titles == ["차주 expression_db 회의·exDB/KEA 미팅"]


def test_new_duplicate_event_merges_existing_due_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    existing = _proposal(
        "proposal/next-week-progress",
        "차주 expression_db 회의·exDB/KEA 미팅",
        status="approved",
        due_date=date(2026, 6, 5),
    )
    store.save_proposal(replace(existing, time_window="10:00"))
    event_draft = ProposalDraft(
        source_key="proposal/next-week-meeting",
        raw_text="차주 expression_db 회의·exDB/KEA 미팅",
        title="차주 expression_db 회의·exDB/KEA 미팅",
        discussion_id="slack/DTEST",
        message_id="slack/DTEST/new",
        line_number=1,
        speaker="me",
        assigned_to="me",
        task_management_area="work",
        scheduled_date=date(2026, 6, 5),
        time_window="10:00",
        item_type="event",
        metadata={"participants": "me", "external_participants": "최지인 선생님", "location_optional": "true"},
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDraftAgent((event_draft,))).handle_message(
        IncomingMessage(
            message_id="slack/DTEST/new",
            sender_id="me",
            chat_id="DTEST",
            visibility="private",
            text="next meeting",
            received_at=datetime(2026, 6, 4, 9, 0),
        )
    )

    canonical = store.get_proposal("proposal/next-week-meeting")
    duplicate = store.get_proposal("proposal/next-week-progress")
    assert canonical is not None
    assert duplicate is not None
    assert canonical.kind == "event"
    assert canonical.status == "approved"
    assert duplicate.status == "rejected"
    assert duplicate.metadata["merged_into_proposal_id"] == canonical.proposal_id
    assert result.outbound_messages
    assert result.outbound_messages[0].message_type == "proposal_approved"


def test_existing_backfill_links_completion_source_under_workflow_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = replace(
        _proposal("proposal/review", "ai-study 발표자료 리뷰 논의", status="approved"),
        kind="event",
        scheduled_date=date(2026, 5, 28),
        metadata={"workflow_role": "parent", "workflow_title": "제3회 ai-study", "workflow_container": "true"},
    )
    decision_child = replace(
        _proposal("proposal/date-decision", "제3회 ai-study 일정 결정", status="done"),
        kind="event",
        scheduled_date=date(2026, 6, 2),
        time_window="14:00",
        metadata={
            "parent_proposal_id": root.proposal_id,
            "linked_completion_source_proposal_id": "proposal/study-done",
            "completed_at": "2026-06-03T10:33:26",
        },
    )
    study_done = replace(
        _proposal("proposal/study-done", "6/2 스터디 진행", status="done"),
        kind="event",
        scheduled_date=date(2026, 6, 2),
        time_window="14:00",
        metadata={"completed_at": "2026-06-03T10:26:54"},
    )
    for proposal in (root, decision_child, study_done):
        store.save_proposal(proposal)

    normalized = normalize_existing_proposal_graph(store.list_proposals(), normalized_at=datetime(2026, 6, 3, 14, 0))
    updates = {proposal.proposal_id: proposal for proposal in normalized.updated_existing}

    linked = updates[study_done.proposal_id]
    assert linked.metadata["parent_proposal_id"] == root.proposal_id
    assert linked.metadata["depends_on_proposal_ids"] == decision_child.proposal_id
    assert linked.metadata["workflow_title"] == "제3회 ai-study"
    assert linked.metadata["relation_type"] == "workflow_completion_evidence"


def test_existing_backfill_creates_parent_for_dependency_only_workflow_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _proposal(
        "proposal/materials",
        "이의신청 자료 전달받기",
        status="done",
        due_date=date(2026, 6, 1),
        metadata={"workflow_title": "zeus 이의신청 자료 업로드", "completed_at": "2026-06-01T16:09:01"},
    )
    second = _proposal(
        "proposal/upload",
        "zeus 이의신청 자료 업로드",
        status="done",
        due_date=date(2026, 6, 1),
        metadata={
            "workflow_title": "zeus 이의신청 자료 업로드",
            "depends_on_proposal_ids": first.proposal_id,
            "completed_at": "2026-06-01T16:09:01",
        },
    )
    store.save_proposal(first)
    store.save_proposal(second)

    normalized = normalize_existing_proposal_graph(store.list_proposals(), normalized_at=datetime(2026, 6, 3, 14, 0))

    assert len(normalized.proposals) == 1
    parent = normalized.proposals[0]
    assert parent.title == "zeus 이의신청 자료 업로드"
    assert parent.status == "done"
    assert parent.metadata["workflow_role"] == "parent"
    assert parent.metadata["workflow_backfill_created"] == "true"
    updates = {proposal.proposal_id: proposal for proposal in normalized.updated_existing}
    assert updates[first.proposal_id].metadata["parent_proposal_id"] == parent.proposal_id
    assert updates[first.proposal_id].metadata["step_index"] == "1"
    assert updates[second.proposal_id].metadata["parent_proposal_id"] == parent.proposal_id
    assert updates[second.proposal_id].metadata["step_index"] == "2"


def test_existing_backfill_converts_comma_parent_to_dependencies(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _proposal("proposal/first", "First", status="done")
    second = _proposal("proposal/second", "Second", status="done")
    child = _proposal(
        "proposal/child",
        "Shared result",
        status="done",
        metadata={"parent_proposal_id": f"{first.proposal_id},{second.proposal_id}"},
    )
    for proposal in (first, second, child):
        store.save_proposal(proposal)

    normalized = normalize_existing_proposal_graph(store.list_proposals(), normalized_at=datetime(2026, 6, 3, 14, 0))
    updates = {proposal.proposal_id: proposal for proposal in normalized.updated_existing}

    repaired = updates[child.proposal_id]
    assert "parent_proposal_id" not in repaired.metadata
    assert repaired.metadata["depends_on_proposal_ids"] == f"{first.proposal_id},{second.proposal_id}"
    assert repaired.metadata["relation_type"] == "dependency_only"


def test_home_and_briefing_limit_completed_children_to_recent_two(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = _proposal(
        "proposal/workflow",
        "Workflow",
        due_date=NOW.date(),
        metadata={"workflow_role": "parent", "workflow_container": "true"},
    )
    children = (
        _proposal(
            "proposal/done-old",
            "Old done child",
            status="done",
            due_date=NOW.date(),
            metadata={"parent_proposal_id": parent.proposal_id, "step_index": "1", "step_count": "4", "completed_at": "2026-05-29T09:00:00"},
        ),
        _proposal(
            "proposal/done-recent-a",
            "Recent done child A",
            status="done",
            due_date=NOW.date(),
            metadata={"parent_proposal_id": parent.proposal_id, "step_index": "2", "step_count": "4", "completed_at": "2026-05-29T10:00:00"},
        ),
        _proposal(
            "proposal/done-recent-b",
            "Recent done child B",
            status="done",
            due_date=NOW.date(),
            metadata={"parent_proposal_id": parent.proposal_id, "step_index": "3", "step_count": "4", "completed_at": "2026-05-29T11:00:00"},
        ),
        _proposal(
            "proposal/open",
            "Open child",
            status="awaiting_approval",
            due_date=NOW.date(),
            metadata={"parent_proposal_id": parent.proposal_id, "step_index": "4", "step_count": "4"},
        ),
    )
    store.save_proposal(parent)
    for child in children:
        store.save_proposal(child)

    view = build_slack_home_view(store, now=NOW)
    today_text = next(
        block["text"]["text"]
        for block in view["blocks"]
        if block.get("type") == "section" and block["text"]["text"].startswith("*오늘*")
    )
    assert "하위작업 3/4 완료 · 다음: 4/4 Open child · 완료 1개 숨김" in today_text
    assert "~Recent done child A~" in today_text
    assert "~Recent done child B~" in today_text
    assert "Old done child" not in today_text
    assert "*Open child*" in today_text

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]
    assert "하위작업 3/4 완료 · 다음: 4/4 Open child · 완료 1개 숨김" in briefing.text
    assert "~Recent done child A~" in briefing.text
    assert "~Recent done child B~" in briefing.text
    assert "Old done child" not in briefing.text


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

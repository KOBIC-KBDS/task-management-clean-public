from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.conflict_policy import apply_conflict_policy, parse_conflict_action
from task_management.operating_agent import OperatingAgentDecision, ProposalDraft
from task_management.orchestrator import TeamTaskOrchestrator
import pytest

from task_management.cli import main
from task_management.slack_adapter import (
    FakeSlackWebClient,
    SlackAdapterError,
    SlackDmAdapter,
    SlackDmConfig,
    run_slack_dm_once,
)
from task_management.slack_fast_cycle import run_slack_fast_cycle
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 18, 10, 0, 0)


class SemanticLunchAgent:
    """Test double for the intended LLM semantic extractor.

    It proves the fast cycle only needs a strict semantic decision envelope; the
    deterministic core handles missing slots, approvals, persistence, and reply
    rendering.
    """

    calls = 0

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        self.calls += 1
        assert pending_approval_requests == ()
        assert pending_proposals == ()
        return OperatingAgentDecision(
            action="create_proposals",
            source="semantic_test",
            confidence=0.99,
            rationale="LLM-shaped semantic extraction produced one normalized event draft.",
            proposal_drafts=(
                ProposalDraft(
                    source_key=f"semantic/{message.message_id}/1",
                    raw_text=message.text,
                    title="수요일 점심회식",
                    discussion_id=f"{message.visibility}/{message.chat_id}/{message.message_id}",
                    message_id=f"{message.message_id}/semantic/1",
                    line_number=1,
                    speaker=message.sender_id,
                    assigned_to="me",
                    task_management_area="work",
                    scheduled_date=date(2026, 5, 20),
                    time_window="lunch",
                    item_type="event",
                    metadata={
                        "participants": "me",
                        "location_optional": "true",
                        "needs_exact_time": "true",
                    },
                ),
            ),
        )


class SemanticBusinessTripAgent:
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
            rationale="Structured course notice became one blocking business-trip proposal.",
            proposal_drafts=(
                ProposalDraft(
                    source_key=f"semantic/{message.message_id}/trip",
                    raw_text=message.text,
                    title="KIRD 선임승급예비자 과정 1기 입과 출장",
                    discussion_id=f"{message.visibility}/{message.chat_id}/{message.message_id}",
                    message_id=f"{message.message_id}/semantic/trip",
                    line_number=1,
                    speaker=message.sender_id,
                    assigned_to="me",
                    task_management_area="work",
                    scheduled_date=date(2026, 5, 20),
                    time_window="all_day",
                    item_type="event",
                    metadata={
                        "participants": "me",
                        "location": "덕산 스플라스 리솜 스테이타워 2F SPACE C",
                        "date_window_start": "2026-05-20",
                        "date_window_end": "2026-05-22",
                        "event_scope": "away",
                        "blocks_in_person": "true",
                        "materials": "명함 50장 이상, 개인노트북, 개인텀블러",
                    },
                ),
            ),
        )


class StateLinkedFallbackAgent:
    def __init__(self) -> None:
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
            source="rule_based",
            confidence=0.99,
            rationale="Allow deterministic state-linked fallback after semantic-first no_action.",
        )


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def _slack_message(ts: str, text: str, *, at: datetime = NOW) -> dict[str, str]:
    return {
        "channel": "DTEST",
        "ts": ts,
        "user": "UUSER",
        "text": text,
        "received_at": at.isoformat(timespec="seconds"),
    }


def test_conflict_action_parser_understands_cannot_attend_feedback() -> None:
    assert parse_conflict_action("\uc218\uc694\uc77c \uc810\uc2ec\ud68c\uc2dd\uc740 \ucc38\uc11d\ud558\uc9c0 \ubabb\ud560\uac83\uac19\ub124...") == "not_attending_existing"


def test_fast_cycle_runs_semantic_core_web_path_without_digest_or_reconcile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    agent = SemanticLunchAgent()
    client = FakeSlackWebClient(
        messages=[_slack_message("1000.000001", "수요일 점심회식 참석자 나")],
        channel_id="DTEST",
    )
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client)

    result = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=agent),
        adapter=adapter,
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    proposal = store.list_proposals()[0]
    assert agent.calls == 1
    assert len(result.messages) == 1
    assert result.results[0].proposals[0].proposal_id == proposal.proposal_id
    assert proposal.title == "수요일 점심회식"
    assert proposal.kind == "question"
    assert proposal.status == "awaiting_approval"
    assert proposal.missing_slots == ("time",)
    assert proposal.scheduled_date == date(2026, 5, 20)
    assert proposal.metadata["participants"] == "me"
    assert result.poll.outbound_messages[0].message_type == "approval_request"
    assert client.sent[0][1] == "2000.000001"

    dashboard = (tmp_path / "out" / "dashboard.html").read_text(encoding="utf-8")
    assert "수요일 점심회식" in dashboard

    event_types = [event["type"] for event in store.read_events()]
    assert "agent.decision.created" in event_types
    assert "slack.fast_cycle.completed" in event_types
    assert "slack.task_cycle.digest.created" not in event_types
    assert "slack.message.reconciled" not in event_types


def test_fast_cycle_can_refresh_slack_home_after_state_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(
        messages=[_slack_message("1000.000001", "수요일 점심회식 참석자 나")],
        channel_id="DTEST",
    )
    adapter = SlackDmAdapter(
        SlackDmConfig(actor_id="me", dm_channel_id="DTEST", user_id="UUSER"),
        client,
    )

    run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=SemanticLunchAgent()),
        adapter=adapter,
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
        home_dashboard_url="http://127.0.0.1:8766/dashboard.html",
    )

    assert len(client.home_views) == 1
    user_id, view, _view_id = client.home_views[0]
    assert user_id == "UUSER"
    assert view["type"] == "home"
    assert "수요일 점심회식" in str(view)
    event = next(item for item in store.read_events() if item["type"] == "slack.fast_cycle.completed")
    assert event["payload"]["home_published"] is True


def test_fast_cycle_reasks_stale_pending_missing_info_with_current_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = Proposal(
        proposal_id="proposal/terms",
        source_message_id="slack/DTEST/terms",
        proposer_id="me",
        title="약관 2차 수정안 검토 및 교육페이지 약관 추가",
        raw_text="약관 확인 필요",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="DTEST",
        message_id="slack/DTEST/terms",
        required_approvers=("me",),
        missing_slots=("exact_date",),
        time_window="14:00",
        created_at=NOW.replace(hour=8),
        updated_at=NOW.replace(hour=9),
        metadata={"needs_exact_date": "true", "participants": "me"},
    )
    store.save_proposal(proposal)
    store.save_approval_request(
        ApprovalRequest(
            request_id="approval/terms",
            proposal_id=proposal.proposal_id,
            approver_id="me",
            requested_at=NOW.replace(hour=8),
        )
    )
    client = FakeSlackWebClient(messages=[], channel_id="DTEST")
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client)

    run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=SemanticLunchAgent()),
        adapter=adapter,
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )
    run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=SemanticLunchAgent()),
        adapter=adapter,
        now=NOW.replace(minute=1),
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    assert len(client.sent) == 1
    assert "약관 2차 수정안 검토 및 교육페이지 약관 추가" in client.sent[0][2]
    assert "정확한 날짜" in client.sent[0][2]
    assert "변경 approval/terms" in client.sent[0][2]
    events = [event for event in store.read_events() if event["type"] == "slack.fast_cycle.completed"]
    assert events[0]["payload"]["pending_missing_info_followup_count"] == 1
    assert events[1]["payload"]["pending_missing_info_followup_count"] == 0


def test_fast_cycle_creates_missing_request_for_repaired_pending_item(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = Proposal(
        proposal_id="proposal/study-group",
        source_message_id="slack/DTEST/ai",
        proposer_id="me",
        title="제3회 study-group 주제 논의",
        raw_text="study-group 논의",
        kind="event",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="DTEST",
        message_id="slack/DTEST/ai",
        required_approvers=("me",),
        missing_slots=("time",),
        scheduled_date=date(2026, 5, 20),
        time_window="오전",
        created_at=NOW.replace(hour=8),
        updated_at=NOW.replace(hour=9),
        metadata={"needs_exact_time": "true", "participants": "me", "location_optional": "true"},
    )
    store.save_proposal(proposal)
    client = FakeSlackWebClient(messages=[], channel_id="DTEST")
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client)

    run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=SemanticLunchAgent()),
        adapter=adapter,
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    requests = store.list_approval_requests(proposal_id=proposal.proposal_id, status="pending")
    assert len(requests) == 1
    assert len(client.sent) == 1
    assert "제3회 study-group 주제 논의" in client.sent[0][2]
    assert "정확한 시간" in client.sent[0][2]


def test_fast_cycle_uses_last_ts_and_does_not_resend_on_duplicate_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    agent = SemanticLunchAgent()
    client = FakeSlackWebClient(
        messages=[_slack_message("1000.000001", "수요일 점심회식 참석자 나")],
        channel_id="DTEST",
    )
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST")

    run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=agent),
        adapter=SlackDmAdapter(config, client),
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )
    duplicate = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=agent),
        adapter=SlackDmAdapter(config, client, oldest=store.get_integration_state("slack.dm.me.last_ts") or ""),
        now=NOW.replace(minute=5),
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    assert duplicate.messages == ()
    assert duplicate.results == ()
    assert store.get_integration_state("slack.dm.me.last_ts") == "1000.000001"
    assert len(client.sent) == 1


def test_fast_cycle_mirrors_team_room_confirmations_to_personal_dm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(
        messages=[_slack_message("1000.250001", "내가 내일 보고서 확인할게")],
        channel_id="DTEST",
    )

    result = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store),
        adapter=SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client),
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    assert result.poll.outbound_messages[0].surface == "team_room"
    assert len(client.sent) == 1
    assert "[팀 공유 기록]" not in client.sent[0][2]
    assert "보고서 확인" in client.sent[0][2]
    # The fallback dedupe key is salted with the triggering inbound message id so
    # distinct inbound messages that reduce to the same stable key are not
    # collapsed (BUG A1). The delivery is still recorded — under the salted key.
    source_message_id = result.poll.messages[0].message_id
    assert store.has_outbound_delivery(
        f"slack-outbound/me/{result.poll.outbound_messages[0].message_type}"
        f"/{result.poll.outbound_messages[0].proposal_id}/{source_message_id}"
    )
    sent_event = next(event for event in store.read_events() if event["type"] == "slack.message.sent")
    assert sent_event["payload"]["original_surface"] == "team_room"
    assert sent_event["payload"]["delivery_surface"] == "slack_personal_dm"
    assert sent_event["payload"]["recipient_id"] == "me"


def test_fast_cycle_holds_blocking_trip_when_it_conflicts_with_approved_lunch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        Proposal(
            proposal_id="proposal/lunch",
            source_message_id="slack/DTEST/999.000001",
            proposer_id="me",
            title="수요일 점심회식",
            raw_text="수요일 점심회식: 오전 11시 30분 편백연가 도룡점",
            kind="event",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="private/DTEST/slack/DTEST/999.000001",
            message_id="slack/DTEST/999.000001",
            required_approvers=("me",),
            approvals=("me",),
            scheduled_date=date(2026, 5, 20),
            time_window="11:30",
            created_at=NOW,
            updated_at=NOW,
            metadata={"participants": "me", "location": "편백연가 도룡점"},
        )
    )
    client = FakeSlackWebClient(
        messages=[_slack_message("1001.000001", "KIRD 과정 출장: 2026. 5. 20.(수) ~ 5. 22.(금)")],
        channel_id="DTEST",
    )

    result = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=SemanticBusinessTripAgent()),
        adapter=SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client),
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    trip = result.results[0].proposals[0]
    assert trip.title == "KIRD 선임승급예비자 과정 1기 입과 출장"
    assert trip.status == "awaiting_approval"
    assert trip.kind == "question"
    assert trip.missing_slots == ("conflict_resolution",)
    assert trip.metadata["conflict_detected"] == "true"
    assert trip.metadata["conflict_with_proposal_ids"] == "proposal/lunch"
    assert result.poll.outbound_messages[0].message_type == "schedule_conflict"
    assert "*수요일 점심회식*" in result.poll.outbound_messages[0].text
    assert "점심회식 불참 처리" in result.poll.outbound_messages[0].text
    assert "출장 중에도 참석 가능" in client.sent[0][2]

    event_types = [event["type"] for event in store.read_events()]
    assert "proposal.conflict_detected" in event_types
    assert [item.title for item in store.list_proposals(status="approved")] == ["수요일 점심회식"]


def test_rule_based_fast_cycle_turns_plain_trip_message_into_conflict_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        Proposal(
            proposal_id="proposal/lunch",
            source_message_id="slack/DTEST/999.000001",
            proposer_id="me",
            title="수요일 점심회식",
            raw_text="수요일 점심회식: 오전 11시 30분 편백연가 도룡점",
            kind="event",
            status="approved",
            assigned_to="me",
            task_management_area="work",
            discussion_id="private/DTEST/slack/DTEST/999.000001",
            message_id="slack/DTEST/999.000001",
            required_approvers=("me",),
            approvals=("me",),
            scheduled_date=date(2026, 5, 20),
            time_window="11:30",
            created_at=NOW,
            updated_at=NOW,
            metadata={"participants": "me", "location": "편백연가 도룡점"},
        )
    )
    client = FakeSlackWebClient(
        messages=[_slack_message("1001.500001", "수~금 출장이라 수요일 점심회식 못 갈 듯")],
        channel_id="DTEST",
    )

    result = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store),
        adapter=SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client),
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    trip = result.results[0].proposals[0]
    assert trip.title == "출장이라 수요일 점심회식 못 갈 듯"
    assert trip.kind == "question"
    assert trip.status == "awaiting_approval"
    assert trip.assigned_to == "me"
    assert trip.scheduled_date == date(2026, 5, 20)
    assert trip.time_window == "all_day"
    assert trip.missing_slots == ("conflict_resolution",)
    assert trip.metadata["date_window_start"] == "2026-05-20"
    assert trip.metadata["date_window_end"] == "2026-05-22"
    assert trip.metadata["event_scope"] == "away"
    assert trip.metadata["conflict_detected"] == "true"
    assert result.poll.outbound_messages[0].message_type == "schedule_conflict"
    assert "_일정 충돌 확인이 필요합니다._" in client.sent[0][2]
    assert "*수요일 점심회식*" in client.sent[0][2]


def _single_day_event(
    proposal_id: str,
    title: str,
    *,
    time_window: str,
    status: str = "approved",
    kind: str = "event",
    when: date = date(2026, 5, 20),
    metadata: dict[str, str] | None = None,
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=f"slack/DTEST/{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind=kind,
        status=status,
        assigned_to="me",
        task_management_area="work",
        discussion_id=f"private/DTEST/{proposal_id}",
        message_id=f"slack/DTEST/{proposal_id}",
        required_approvers=("me",),
        approvals=("me",) if status == "approved" else (),
        scheduled_date=when,
        time_window=time_window,
        created_at=NOW,
        updated_at=NOW,
        metadata={"participants": "me", **(metadata or {})},
    )


def test_same_day_disjoint_exact_times_do_not_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Approved evening 회식 at a precise clock time on the same calendar day.
    store.save_proposal(_single_day_event("proposal/dinner", "저녁 회식", time_window="19:00"))
    # New single-day blocking 교육 in the morning with a precise clock time.
    new_training = _single_day_event(
        "proposal/training",
        "리더십 교육",
        time_window="09:00-12:00",
        status="awaiting_approval",
    )

    held, requests, outbound = apply_conflict_policy(store, new_training, actor_id="me", now=NOW)

    # Disjoint exact times -> no conflict; the proposal is returned untouched.
    assert held is new_training
    assert held.kind == "event"
    assert held.metadata.get("conflict_detected") != "true"
    assert requests == ()
    assert outbound == ()


def test_same_day_overlapping_exact_times_still_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_single_day_event("proposal/dinner", "저녁 회식", time_window="11:30"))
    new_training = _single_day_event(
        "proposal/training",
        "리더십 교육",
        time_window="11:00-13:00",
        status="awaiting_approval",
    )

    held, requests, outbound = apply_conflict_policy(store, new_training, actor_id="me", now=NOW)

    assert held.kind == "question"
    assert held.metadata["conflict_detected"] == "true"
    assert held.metadata["conflict_with_proposal_ids"] == "proposal/dinner"
    assert held.approvals == ()
    assert requests and outbound


def test_same_day_period_only_token_still_conflicts_conservatively(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Existing side carries a period-only token (오후) -> conservative date-level hold.
    store.save_proposal(_single_day_event("proposal/dinner", "오후 회식", time_window="오후"))
    new_training = _single_day_event(
        "proposal/training",
        "리더십 교육",
        time_window="09:00-12:00",
        status="awaiting_approval",
    )

    held, requests, outbound = apply_conflict_policy(store, new_training, actor_id="me", now=NOW)

    assert held.kind == "question"
    assert held.metadata["conflict_detected"] == "true"
    assert held.metadata["conflict_with_proposal_ids"] == "proposal/dinner"


def test_conflict_feedback_marks_old_event_not_attending_and_approves_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lunch = Proposal(
        proposal_id="proposal/lunch",
        source_message_id="slack/DTEST/999.000001",
        proposer_id="me",
        title="수요일 점심회식",
        raw_text="수요일 점심회식: 오전 11시 30분 편백연가 도룡점",
        kind="event",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/999.000001",
        message_id="slack/DTEST/999.000001",
        required_approvers=("me",),
        approvals=("me",),
        scheduled_date=date(2026, 5, 20),
        time_window="11:30",
        created_at=NOW,
        updated_at=NOW,
        metadata={"participants": "me", "location": "편백연가 도룡점"},
    )
    trip = Proposal(
        proposal_id="proposal/trip",
        source_message_id="slack/DTEST/1001.000001",
        proposer_id="me",
        title="KIRD 선임승급예비자 과정 1기 입과 출장",
        raw_text="KIRD 과정 출장: 2026. 5. 20.(수) ~ 5. 22.(금)",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/1001.000001",
        message_id="slack/DTEST/1001.000001/semantic/trip",
        required_approvers=("me",),
        missing_slots=("conflict_resolution",),
        scheduled_date=date(2026, 5, 20),
        time_window="all_day",
        created_at=NOW,
        updated_at=NOW,
        metadata={
            "participants": "me",
            "date_window_start": "2026-05-20",
            "date_window_end": "2026-05-22",
            "conflict_detected": "true",
            "conflict_with_proposal_ids": lunch.proposal_id,
            "blocks_in_person": "true",
        },
    )
    request = ApprovalRequest(
        request_id="approval/trip",
        proposal_id=trip.proposal_id,
        approver_id="me",
        requested_at=NOW,
    )
    store.save_proposal(lunch)
    store.save_proposal(trip)
    store.save_approval_request(request)
    client = FakeSlackWebClient(
        messages=[_slack_message("1002.000001", "수요일 점심회식은 참석하지 못할것같네...")],
        channel_id="DTEST",
    )

    result = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=StateLinkedFallbackAgent()),
        adapter=SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client),
        now=NOW.replace(minute=10),
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    updated_trip = store.get_proposal(trip.proposal_id)
    updated_lunch = store.get_proposal(lunch.proposal_id)
    assert updated_trip is not None
    assert updated_lunch is not None
    assert updated_trip.status == "approved"
    assert updated_trip.kind == "event"
    assert updated_trip.missing_slots == ()
    assert updated_trip.metadata["conflict_resolution_action"] == "not_attending_existing"
    assert updated_lunch.status == "rejected"
    assert updated_lunch.metadata["attendance_status"] == "not_attending"
    assert store.get_approval_request(request.request_id).status == "accepted"  # type: ignore[union-attr]
    assert result.poll.outbound_messages[0].message_type == "conflict_resolved"
    assert "점심회식" in client.sent[0][2]
    assert "출장" in client.sent[0][2]


def test_contextual_feedback_updates_the_referenced_pending_tasks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 5, 19, 16, 8, 0)
    packing = Proposal(
        proposal_id="proposal/packing",
        source_message_id="slack/DTEST/1000.000001",
        proposer_id="me",
        title="출장 준비 자료 싸기",
        raw_text="내일 출장 전에 오늘 밤 준비 자료 싸기",
        kind="event",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/1000.000001",
        message_id="slack/DTEST/1000.000001/semantic/1",
        required_approvers=("me",),
        approvals=("me",),
        scheduled_date=date(2026, 5, 19),
        time_window="night",
        created_at=now,
        updated_at=now,
        metadata={"participants": "me", "location_optional": "true"},
    )
    before_trip = Proposal(
        proposal_id="proposal/before-trip",
        source_message_id="slack/DTEST/1001.000001",
        proposer_id="me",
        title="출장 전 다른 ProjectA담당자 및 DataPortal담당자와 할일 정리하기",
        raw_text="내일 출장 전 오늘 다른 ProjectA담당자 및 DataPortal담당자와 이번주 할일 정리하기",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/1001.000001",
        message_id="slack/DTEST/1001.000001/semantic/1",
        required_approvers=("me",),
        missing_slots=("conflict_resolution",),
        scheduled_date=date(2026, 5, 19),
        time_window="all_day",
        created_at=now,
        updated_at=now,
        metadata={
            "participants": "me",
            "conflict_detected": "true",
            "conflict_with_proposal_ids": packing.proposal_id,
            "conflict_policy": "ask_before_mutating_existing_events",
            "blocks_in_person": "true",
        },
    )
    return_check = Proposal(
        proposal_id="proposal/return-check",
        source_message_id="slack/DTEST/1001.000001",
        proposer_id="me",
        title="차주 복귀해서 확인할 일들 정리해두기",
        raw_text="차주 복귀해서 확인할 일들 정리해두기",
        kind="event",
        status="awaiting_approval",
        assigned_to="unassigned",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/1001.000001",
        message_id="slack/DTEST/1001.000001/semantic/2",
        required_approvers=("me",),
        missing_slots=("assigned_to",),
        scheduled_date=date(2026, 5, 19),
        time_window="06:00",
        created_at=now,
        updated_at=now,
        metadata={"location": ")전에 할 일이야"},
    )
    store.save_proposal(packing)
    store.save_proposal(before_trip)
    store.save_proposal(return_check)
    store.save_approval_request(
        ApprovalRequest(
            request_id="approval/before-trip",
            proposal_id=before_trip.proposal_id,
            approver_id="me",
            requested_at=now,
        )
    )
    store.save_approval_request(
        ApprovalRequest(
            request_id="approval/return-check",
            proposal_id=return_check.proposal_id,
            approver_id="me",
            requested_at=now,
        )
    )

    result = TeamTaskOrchestrator(store, operating_agent=StateLinkedFallbackAgent()).handle_message(
        IncomingMessage(
            message_id="slack/DTEST/1002.000001",
            sender_id="me",
            chat_id="DTEST",
            visibility="private",
            text="이 논의는 오늘 퇴근(6시)전에 할 일이야. 차주 복귀해서는 내가 담당자이고, 일시는 월요일 오후 2시경이 좋겠네.",
            received_at=now.replace(minute=9),
        )
    )

    updated_before_trip = store.get_proposal(before_trip.proposal_id)
    updated_return_check = store.get_proposal(return_check.proposal_id)
    assert updated_before_trip is not None
    assert updated_return_check is not None
    assert {proposal.proposal_id for proposal in result.proposals} == {
        before_trip.proposal_id,
        return_check.proposal_id,
    }
    assert updated_before_trip.kind == "task"
    assert updated_before_trip.status == "approved"
    assert updated_before_trip.due_date == date(2026, 5, 19)
    assert updated_before_trip.scheduled_date is None
    assert updated_before_trip.time_window == "18:00"
    assert updated_before_trip.missing_slots == ()
    assert updated_before_trip.metadata.get("conflict_detected") != "true"
    assert updated_before_trip.metadata.get("location", "") == ""
    assert store.get_approval_request("approval/before-trip").status == "accepted"  # type: ignore[union-attr]

    assert updated_return_check.status == "approved"
    assert updated_return_check.kind == "task"
    assert updated_return_check.assigned_to == "me"
    assert updated_return_check.due_date == date(2026, 5, 25)
    assert updated_return_check.scheduled_date is None
    assert updated_return_check.time_window == "14:00"
    assert updated_return_check.missing_slots == ()
    assert updated_return_check.metadata.get("location", "") == ""
    assert store.get_approval_request("approval/return-check").status == "accepted"  # type: ignore[union-attr]

    sent_text = "\n".join(message.text for message in result.outbound_messages)
    assert "출장 전 다른 ProjectA담당자 및 DataPortal담당자와 할일 정리하기 (2026-05-19 18:00)" in sent_text
    assert "차주 복귀해서 확인할 일들 정리해두기 (2026-05-25 14:00)" in sent_text
    assert "06:00" not in sent_text
    assert ")전에 할 일이야" not in sent_text


# --- BUG #15: a blocked-send instance fails closed BEFORE polling/last_ts advance ---


def test_fast_cycle_send_refuses_before_polling_when_instance_guard_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ALLOWED_INSTANCE_ID set but INSTANCE_ID empty => can_send is False. A live
    # --send fast-cycle must refuse BEFORE polling so the inbound DM is never
    # marked seen and last_ts never advances (otherwise the reply is lost).
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DTEST")
    monkeypatch.setenv("TASK_MANAGEMENT_ALLOWED_INSTANCE_ID", "primary")
    monkeypatch.delenv("TASK_MANAGEMENT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("SLACK_USER_ID", raising=False)

    state_dir = tmp_path / "state"

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--state",
                str(state_dir),
                "slack-fast-cycle",
                "--now",
                NOW.isoformat(),
                "--dashboard-output",
                str(tmp_path / "out" / "dashboard.html"),
                "--send",
            ]
        )

    assert "refused before polling" in str(excinfo.value)

    # Nothing was polled, recorded, or advanced: no state and no polled events.
    store = _store(state_dir)
    assert store.get_integration_state("slack.dm.me.last_ts") is None
    polled_events = [event for event in store.read_events() if event["type"] == "slack.message.polled"]
    assert polled_events == []


class _SendAlwaysFailsClient(FakeSlackWebClient):
    def send_message(self, channel_id: str, text: str) -> str:
        raise SlackAdapterError("transient Slack send failure")


def test_run_slack_dm_once_keeps_last_ts_repollable_when_send_fails(tmp_path: Path) -> None:
    # A send-side failure after polling must NOT advance last_ts, so the inbound
    # message stays re-pollable instead of being permanently marked consumed.
    store = _store(tmp_path)
    client = _SendAlwaysFailsClient(
        messages=[_slack_message("1000.000001", "내가 내일 보고서 확인할게")],
        channel_id="DTEST",
    )
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST")

    with pytest.raises(SlackAdapterError):
        run_slack_dm_once(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(config, client),
            send=True,
            now=NOW,
        )

    # last_ts must be unchanged so the next clean cycle re-polls the same message.
    assert store.get_integration_state("slack.dm.me.last_ts") is None
    # The failed reply is still queued (pending), not lost.
    assert len(store.list_pending_outbound_messages(provider="slack")) == 1

    # Once the send transport recovers, the next cycle re-polls the still-unseen
    # message and drains the pending reply, delivering the previously-lost answer.
    healthy_client = FakeSlackWebClient(
        messages=[_slack_message("1000.000001", "내가 내일 보고서 확인할게")],
        channel_id="DTEST",
    )
    retry = run_slack_fast_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store),
        adapter=SlackDmAdapter(config, healthy_client),
        now=NOW.replace(minute=5),
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
    )

    assert len(retry.messages) == 1
    assert len(healthy_client.sent) == 1
    assert store.list_pending_outbound_messages(provider="slack") == ()
    assert store.get_integration_state("slack.dm.me.last_ts") == "1000.000001"

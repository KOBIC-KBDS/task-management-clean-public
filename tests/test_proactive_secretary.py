from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import pytest

from task_management.approval_policy import approval_request
from task_management.cli import main
from task_management.conflict_policy import apply_conflict_resolution_feedback
from task_management.deferred_policy import default_deferred_until
from task_management.domain import ApprovalRequest, IncomingMessage, OutboundMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision, ProposalPatch
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.secretary import (
    build_afternoon_briefing,
    build_end_of_day_review,
    build_morning_briefing,
    build_proactive_checks,
)
from task_management.slack_adapter import SlackAdapterError, SlackDmAdapter, SlackDmConfig, dispatch_slack_outbound, queue_slack_outbound
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 20, 8, 0, 0)


PROACTIVE_SCENARIOS = (
    {"id": "S01", "family": "intake_slots", "summary": "업무 내용만 들어온 뒤 일시와 장소를 차례로 확정한다."},
    {"id": "S02", "family": "progress", "summary": "반만 진행한 업무가 남은 일과 함께 기록된다."},
    {"id": "S03", "family": "completion", "summary": "자연어 완료 답변이 기존 태스크를 done 처리한다."},
    {"id": "S04", "family": "deferral", "summary": "지연/연기 답변이 due date와 리마인드 기준을 바꾼다."},
    {"id": "S05", "family": "briefing", "summary": "아침 브리핑이 오늘 할 일과 확인 질문을 요약한다."},
    {"id": "S06", "family": "briefing", "summary": "오늘 마감 작업에 대해 완료 여부를 먼저 물어본다."},
    {"id": "S07", "family": "conflict", "summary": "출장과 점심 회식처럼 충돌하는 일정은 처리 방침을 질문한다."},
    {"id": "S08", "family": "ambiguity", "summary": "완료 표현만 있고 대상이 불명확하면 임의 변경하지 않는다."},
    {"id": "S09", "family": "ambiguity", "summary": "장소만 들어오면 가장 그럴듯한 기존 항목에 매칭하거나 후보 질문으로 남긴다."},
    {"id": "S10", "family": "multi_clause", "summary": "한 메시지에 두 항목 피드백이 들어오면 각각의 대상에 반영한다."},
    {"id": "S11", "family": "routine", "summary": "반복 회의는 준비 subtask와 당일 리마인드를 만든다."},
    {"id": "S12", "family": "routine", "summary": "반복 일정의 다음 회차를 주간 브리핑에서 보여준다."},
    {"id": "S13", "family": "collaboration", "summary": "외부 협업자와 나의 역할이 분리되어 metadata에 남는다."},
    {"id": "S14", "family": "collaboration", "summary": "협업 논의 결과물의 후속 검토 일을 별도 task로 유지한다."},
    {"id": "S15", "family": "state", "summary": "같은 Slack ts는 중복 task를 만들지 않는다."},
    {"id": "S16", "family": "state", "summary": "테스트 롤백 지점 이후 생성된 task만 회수 가능해야 한다."},
    {"id": "S17", "family": "secretary", "summary": "비서가 미완료 작업을 주도적으로 확인하고 답장을 안내한다."},
    {"id": "S18", "family": "secretary", "summary": "사용자가 아직 못했다고 답하면 완료 처리하지 않고 다음 확인을 남긴다."},
    {"id": "S19", "family": "export", "summary": "승인/확정된 항목만 task-core preview payload에 들어간다."},
    {"id": "S20", "family": "runtime", "summary": "dry-run 브리핑은 dedupe를 소비하지 않고 send에서만 예약한다."},
)


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def _message(text: str, *, ts: str = "1000.000001", at: datetime = NOW) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"slack/DTEST/{ts}",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text=text,
        received_at=at,
    )


def _proposal(
    proposal_id: str,
    title: str,
    *,
    raw_text: str = "",
    kind: str = "task",
    status: str = "approved",
    assigned_to: str = "me",
    due_date: date | None = None,
    scheduled_date: date | None = None,
    time_window: str = "",
    missing_slots: tuple[str, ...] = (),
    metadata: dict[str, str] | None = None,
    created_at: datetime = NOW,
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=f"slack/DTEST/source-{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=raw_text or title,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        assigned_to=assigned_to,
        task_management_area="work",
        discussion_id=f"private/DTEST/source-{proposal_id}",
        message_id=f"slack/DTEST/source-{proposal_id}/1",
        required_approvers=("me",),
        approvals=("me",) if status in {"approved", "applied", "done"} else (),
        missing_slots=missing_slots,
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=time_window,
        created_at=created_at,
        updated_at=created_at,
        metadata={"participants": "me", **(metadata or {})},
    )


def _save_pending(store: TeamTaskStore, proposal: Proposal, request_id: str) -> None:
    store.save_proposal(proposal)
    store.save_approval_request(
        ApprovalRequest(
            request_id=request_id,
            proposal_id=proposal.proposal_id,
            approver_id="me",
            requested_at=proposal.created_at,
        )
    )


class MalformedCompletionPatchAgent:
    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        proposal = pending_proposals[0]
        return OperatingAgentDecision(
            action="apply_feedback",
            source="test_model",
            confidence=1.0,
            rationale="Malformed completion patch for validation regression.",
            proposal_patches=(
                ProposalPatch(
                    request_id="",
                    proposal_id=proposal.proposal_id,
                    actor_id=message.sender_id,
                    body=message.text,
                    temporal_update={"semantic_update_type": "completion"},
                    reason="malformed_completion",
                    target_confidence=0.9,
                    evidence_text=message.text,
                ),
            ),
        )


class DirectPatchAgent:
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
            source="test_model",
            confidence=1.0,
            rationale="Direct semantic patch regression.",
            proposal_patches=(self.patch,),
        )


class FailingSlackClient:
    def open_dm(self, user_id: str) -> str:
        return "DTEST"

    def read_channel(self, channel_id: str, *, oldest: str = "", limit: int = 100) -> tuple[dict[str, Any], ...]:
        return ()

    def send_message(self, channel_id: str, text: str) -> str:
        raise SlackAdapterError("transient Slack failure")


class SuccessfulSlackClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def open_dm(self, user_id: str) -> str:
        return "DTEST"

    def read_channel(self, channel_id: str, *, oldest: str = "", limit: int = 100) -> tuple[dict[str, Any], ...]:
        return ()

    def send_message(self, channel_id: str, text: str) -> str:
        ts = f"3000.{len(self.sent) + 1:06d}"
        self.sent.append((channel_id, ts, text))
        return ts


def test_twenty_proactive_secretary_scenarios_are_mapped_to_representative_families() -> None:
    assert len(PROACTIVE_SCENARIOS) == 20
    assert len({scenario["id"] for scenario in PROACTIVE_SCENARIOS}) == 20
    families = {scenario["family"] for scenario in PROACTIVE_SCENARIOS}
    assert {"intake_slots", "progress", "completion", "deferral", "briefing", "secretary"} <= families


@pytest.mark.parametrize("scenario", PROACTIVE_SCENARIOS, ids=[scenario["id"] for scenario in PROACTIVE_SCENARIOS])
def test_twenty_scenario_matrix_executes_a_policy_smoke(tmp_path: Path, scenario: dict[str, str]) -> None:
    store = _store(tmp_path)
    family = scenario["family"]

    if family == "completion":
        store.save_proposal(_proposal("proposal/work", "출장 준비물 싸기", due_date=NOW.date()))
        TeamTaskOrchestrator(store).handle_message(_message("출장 준비물 다 쌌어"))
        assert store.get_proposal("proposal/work").status == "done"  # type: ignore[union-attr]
    elif family == "progress":
        store.save_proposal(_proposal("proposal/work", "리뷰위원회 발표자료 제작", due_date=NOW.date()))
        TeamTaskOrchestrator(store).handle_message(_message("리뷰위원회 발표자료 반쯤 했고 검토만 남음"))
        updated = store.get_proposal("proposal/work")
        assert updated is not None and updated.metadata["progress_status"] == "partial"
    elif family == "deferral":
        store.save_proposal(_proposal("proposal/work", "ProjectA 후속 정리", due_date=NOW.date()))
        TeamTaskOrchestrator(store).handle_message(_message("ProjectA 후속 정리는 금요일 오후로 미뤄줘"))
        updated = store.get_proposal("proposal/work")
        assert updated is not None and updated.due_date == date(2026, 5, 22)
    elif family == "ambiguity":
        store.save_proposal(_proposal("proposal/a", "출장 준비물 싸기", due_date=NOW.date()))
        store.save_proposal(_proposal("proposal/b", "발표자료 제작", due_date=NOW.date()))
        TeamTaskOrchestrator(store).handle_message(_message("다 했어"))
        assert {proposal.status for proposal in store.list_proposals()} == {"approved"}
    elif family == "state":
        orchestrator = TeamTaskOrchestrator(store)
        first = orchestrator.handle_message(_message("개인 예약 예약 확인해야 해", ts="state.000001"))
        duplicate = orchestrator.handle_message(_message("개인 예약 예약 확인해야 해", ts="state.000001"))
        assert first.ignored_duplicate is False
        assert duplicate.ignored_duplicate is True
    elif family in {"briefing", "secretary", "runtime"}:
        store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date()))
        messages = build_proactive_checks(store, now=NOW, actor_id="me", reserve=False)
        assert len(messages) == 1 and "오늘 보고서 정리" in messages[0].text
        assert store.has_outbound_delivery(messages[0].card["dedupe_key"]) is False
    else:
        store.save_proposal(_proposal("proposal/context", scenario["summary"], due_date=NOW.date()))
        briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)
        assert len(briefing) == 1
        assert scenario["summary"] in briefing[0].text


def test_duplicate_outbound_dedupe_records_skip_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST", bot_token="test-token"))
    message = OutboundMessage(
        surface="personal_chat",
        recipient_id="me",
        message_type="briefing",
        text="Hello",
        card={"dedupe_key": "test/dedupe"},
    )
    store.record_outbound_delivery(
        dedupe_key="test/dedupe",
        surface="personal_chat",
        recipient_id="me",
        provider="slack",
        provider_message_id="111.222",
        sent_at=NOW,
        payload={"text": "Hello"},
    )

    queued = queue_slack_outbound(store, adapter, (message,), queued_at=NOW)

    assert queued == 0
    skipped = [event for event in store.read_events() if event["type"] == "slack.message.skipped"]
    assert skipped[-1]["payload"]["reason"] == "duplicate_dedupe_key"
    assert skipped[-1]["payload"]["dedupe_key"] == "test/dedupe"


def test_morning_briefing_summarizes_today_pending_and_dedupes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/meeting",
            "sample-data sync 미팅",
            kind="event",
            scheduled_date=NOW.date(),
            time_window="10:00",
            metadata={"participants": "me,김담당", "location": "3층 회의실"},
        )
    )
    store.save_proposal(
        _proposal(
            "proposal/packing",
            "출장 준비물 싸기",
            due_date=NOW.date(),
            time_window="morning",
        )
    )
    _save_pending(
        store,
        _proposal(
            "proposal/babyfair",
            "Example Lab 워크숍 날짜 정하기",
            kind="question",
            status="awaiting_approval",
            assigned_to="shared",
            missing_slots=("exact_date", "participants"),
            metadata={"participants": "me,teammate"},
        ),
        "approval/babyfair",
    )

    first = build_morning_briefing(
        store,
        now=NOW,
        actor_id="me",
        dashboard_url="http://127.0.0.1:8787/dashboard.html",
        reserve=True,
    )
    duplicate = build_morning_briefing(store, now=NOW.replace(minute=1), actor_id="me", reserve=True)

    assert len(first) == 1
    assert duplicate == ()
    text = first[0].text
    assert "오늘 아침 브리핑" in text
    assert "sample-data sync 미팅" in text
    assert "출장 준비물 싸기" in text
    assert "오늘 답장/확인 필요한 항목" in text
    assert "Example Lab 워크숍 날짜 정하기" in text
    assert "웹 task page" in text
    assert "출장 준비물 다 쌌어" in text
    assert first[0].card["attention_count"] == "2"
    assert any(event["type"] == "briefing.morning.created" for event in store.read_events())


def test_morning_briefing_attention_includes_due_and_overdue_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date(), time_window="15:00"))
    store.save_proposal(_proposal("proposal/early", "오전 우선 확인", due_date=NOW.date(), time_window="09:00"))
    store.save_proposal(_proposal("proposal/overdue", "어제 회의록 정리", due_date=date(2026, 5, 19)))

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "오늘 답장/확인 필요한 항목" in briefing.text
    assert "*오늘 보고서 정리*: 기한: 2026년 5월 20일(수) 15:00. 오늘 확인할 작업입니다" in briefing.text
    assert briefing.text.index("오전 우선 확인") < briefing.text.index("오늘 보고서 정리")
    assert briefing.text.index("오늘 보고서 정리") < briefing.text.index("어제 회의록 정리")
    assert "- 2026년 5월 19일(화)까지 어제 회의록 정리를 진행하면 됩니다. 담당자는 사용자님입니다." in briefing.text
    assert "- 🔴 *어제 회의록 정리*" not in briefing.text
    assert "어제 회의록 정리를 진행하면 됩니다. 담당자는 사용자님입니다. `overdue` · 🔴 마감 지남" in briefing.text
    assert "*어제 회의록 정리*: 기한: 2026년 5월 19일(화) · 🔴 마감 지남. 마감이 지난 작업입니다" in briefing.text
    assert briefing.card["attention_count"] == "3"


def test_afternoon_briefing_summarizes_today_and_week_remaining_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/today", "오후 미팅 준비", due_date=NOW.date(), time_window="afternoon"))
    store.save_proposal(_proposal("proposal/week", "이번 주 보고서 정리", due_date=date(2026, 5, 22)))
    store.save_proposal(_proposal("proposal/week-early", "내일 오전 확인", due_date=date(2026, 5, 21), time_window="09:00"))
    _save_pending(
        store,
        _proposal(
            "proposal/pending",
            "회의 장소 정하기",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date(),
            missing_slots=("location",),
        ),
        "approval/pending",
    )

    first = build_afternoon_briefing(
        store,
        now=NOW.replace(hour=13),
        actor_id="me",
        dashboard_url="http://127.0.0.1:8766/dashboard.html",
        reserve=True,
    )
    duplicate = build_afternoon_briefing(store, now=NOW.replace(hour=13, minute=5), actor_id="me", reserve=True)

    assert len(first) == 1
    assert duplicate == ()
    text = first[0].text
    assert "오후 1시 브리핑" in text
    assert "오후 미팅 준비" in text
    assert "이번 주 보고서 정리" in text
    assert text.index("내일 오전 확인") < text.index("이번 주 보고서 정리")
    assert "회의 장소 정하기" in text
    assert "웹 task page" in text
    assert first[0].message_type == "afternoon_briefing"
    assert first[0].card["today_open_count"] == "2"
    assert first[0].card["week_open_count"] == "2"
    assert any(event["type"] == "briefing.afternoon.created" for event in store.read_events())


def test_morning_briefing_does_not_repeat_due_task_in_confirmed_section(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/due", "출장 준비물 싸기", due_date=NOW.date()))

    text = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0].text

    assert text.count("출장 준비물 싸기") == 2
    assert "- 오늘 날짜로 확정된 일정/작업은 없습니다." in text


def test_morning_briefing_attention_includes_deferred_due_missing_info(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/lunch",
            "수요일 점심회식",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date(),
            time_window="lunch",
            missing_slots=("time",),
            metadata={
                "participants": "me",
                "deferred_until": NOW.replace(hour=7).isoformat(timespec="seconds"),
                "deferred_missing_slots": "time",
            },
        ),
        "approval/lunch",
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "다시 확인할 항목입니다" in briefing.text
    assert "수요일 점심회식" in briefing.text
    assert "*정확한 시간*" in briefing.text
    assert briefing.card["attention_count"] == "1"
    assert briefing.card["deferred_due_count"] == "1"


def test_future_deferred_pending_missing_info_is_quiet_until_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/future-lunch",
            "금요일 점심회식",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date(),
            time_window="lunch",
            missing_slots=("time",),
            metadata={
                "participants": "me",
                "deferred_until": date(2026, 5, 21).isoformat() + "T08:30:00",
                "deferred_missing_slots": "time",
            },
        ),
        "approval/future-lunch",
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "금요일 점심회식" not in briefing.text
    assert "다시 확인할 항목입니다" not in briefing.text
    assert briefing.card["attention_count"] == "0"
    assert briefing.card["deferred_due_count"] == "0"


def test_malformed_deferred_until_is_quiet_not_fired_immediately(tmp_path: Path) -> None:
    # Regression for BUG #20: a non-ISO deferred_until ('next week', a value with
    # a trailing 'KST') previously parsed-failed and was treated as 'due now', so
    # a malformed deferral fired an immediate reminder. The conservative fix keeps
    # it quiet until the value is corrected to a parseable timestamp.
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/malformed-lunch",
            "월요일 점심회식",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date(),
            time_window="lunch",
            missing_slots=("time",),
            metadata={
                "participants": "me",
                "deferred_until": "2026-06-13 09:00 KST",
                "deferred_missing_slots": "time",
            },
        ),
        "approval/malformed-lunch",
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "월요일 점심회식" not in briefing.text
    assert "다시 확인할 항목입니다" not in briefing.text
    assert briefing.card["attention_count"] == "0"
    assert briefing.card["deferred_due_count"] == "0"


def test_same_day_default_deferral_surfaces_in_morning_briefing(tmp_path: Path) -> None:
    # Regression for BUG A9: a deferral made on a prior day targeting today must
    # surface in today's ~08:00 briefing. The default policy previously parked
    # the re-prompt at 08:30, after the once-a-day briefing window had already
    # fired and locked out the day via its dedupe key, so the user was never
    # re-prompted. The policy must land it at/before the briefing window.
    deferred_until = default_deferred_until(NOW.date(), changed_at=NOW.replace(day=18, hour=10))
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/today-lunch",
            "수요일 점심회식",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date(),
            time_window="lunch",
            missing_slots=("time",),
            metadata={
                "participants": "me",
                "deferred_until": deferred_until,
                "deferred_missing_slots": "time",
            },
        ),
        "approval/today-lunch",
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "다시 확인할 항목입니다" in briefing.text
    assert "수요일 점심회식" in briefing.text
    assert briefing.card["attention_count"] == "1"
    assert briefing.card["deferred_due_count"] == "1"


def test_future_day_default_deferral_is_quiet_at_today_briefing(tmp_path: Path) -> None:
    # The complement of the regression above: a deferral targeting a FUTURE day
    # must stay silent at today's 08:00 briefing (no premature fire), even though
    # the re-prompt now lands earlier in the day.
    deferred_until = default_deferred_until(NOW.date() + timedelta(days=1), changed_at=NOW)
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/future-default-lunch",
            "목요일 점심회식",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date() + timedelta(days=1),
            time_window="lunch",
            missing_slots=("time",),
            metadata={
                "participants": "me",
                "deferred_until": deferred_until,
                "deferred_missing_slots": "time",
            },
        ),
        "approval/future-default-lunch",
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "목요일 점심회식" not in briefing.text
    assert "다시 확인할 항목입니다" not in briefing.text
    assert briefing.card["attention_count"] == "0"
    assert briefing.card["deferred_due_count"] == "0"


def test_supervisor_wires_periodic_reminders_tick() -> None:
    # Regression for BUG A9 part 2: the live supervisor previously scheduled
    # only the once-a-day briefings, so the reminders/deferred_reminder cadence
    # never ran in deployment. The supervisor must invoke the reminders CLI on a
    # within-day interval (not gated to once per calendar day like the briefings)
    # so deferrals surface the moment they become due.
    script = Path(__file__).resolve().parents[1] / "ops" / "slack-secretary-supervisor.sh"
    body = script.read_text(encoding="utf-8")

    # The reminders subcommand is actually invoked.
    assert "task_management.cli --state \"$STATE\" reminders --now" in body
    # It is fired on an interval, not locked to once per day like the briefings.
    assert "REMINDERS_INTERVAL_MINUTES" in body
    # The reminders marker is keyed on a within-day slot, not just "$today".
    assert "reminders_slot" in body
    # reminders must NOT inject --actor (that subcommand rejects it).
    reminders_line = next(line for line in body.splitlines() if "reminders --now" in line)
    assert "--actor" not in reminders_line


def test_due_deferred_missing_info_is_not_duplicated_as_progress_attention(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/deferred-approved",
            "점심 장소 확인",
            due_date=NOW.date(),
            metadata={
                "participants": "me",
                "deferred_until": NOW.replace(hour=7).isoformat(timespec="seconds"),
                "deferred_missing_slots": "location",
            },
        )
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "다시 확인할 항목입니다" in briefing.text
    assert "*장소*" in briefing.text
    assert "오늘 확인할 작업입니다" not in briefing.text
    assert briefing.card["attention_count"] == "1"


def test_future_deferred_approved_due_work_is_not_nagged_before_defer_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/future-deferred-approved",
            "회의 장소 확인",
            due_date=NOW.date(),
            metadata={
                "participants": "me",
                "deferred_until": date(2026, 5, 21).isoformat() + "T08:30:00",
                "deferred_missing_slots": "location",
            },
        )
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]
    checks = build_proactive_checks(store, now=NOW.replace(hour=15), actor_id="me", reserve=False)

    assert "회의 장소 확인" not in briefing.text
    assert briefing.card["attention_count"] == "0"
    assert checks == ()


def test_morning_briefing_attention_includes_pending_approval_decision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/decision",
            "문서 공유 방식 정하기",
            kind="decision",
            status="awaiting_approval",
            missing_slots=(),
        ),
        "approval/decision",
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "*문서 공유 방식 정하기*: 승인/변경/거절 중 어떻게 처리할지 알려주세요" in briefing.text
    assert briefing.card["attention_count"] == "1"


def test_morning_briefing_renders_relation_children_under_visible_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/parent", "인수인계 정리", due_date=NOW.date()))
    store.save_proposal(
        _proposal(
            "proposal/child",
            "환경변수 목록 검토",
            due_date=NOW.date(),
            metadata={
                "participants": "me",
                "parent_proposal_id": "proposal/parent",
                "step_index": "1",
                "step_count": "2",
            },
        )
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "- 2026년 5월 20일(수)까지 인수인계 정리를 진행하면 됩니다" in briefing.text
    assert "  ↳ 하위작업 0/1 완료 · 다음: 1/2 환경변수 목록 검토" in briefing.text
    assert "    └─ [1/2] 환경변수 목록 검토 — ☐ 승인됨 · 마감 2026년 5월 20일(수) · 담당 사용자님" in briefing.text


def test_morning_briefing_subtask_tree_shows_progress_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/parent", "워크플로 점검", due_date=NOW.date()))
    store.save_proposal(
        _proposal(
            "proposal/done-child",
            "완료된 하위작업",
            status="done",
            due_date=NOW.date(),
            metadata={
                "parent_proposal_id": "proposal/parent",
                "step_index": "1",
                "step_count": "2",
            },
        )
    )
    store.save_proposal(
        _proposal(
            "proposal/open-child",
            "남은 하위작업",
            status="awaiting_approval",
            due_date=NOW.date(),
            metadata={
                "parent_proposal_id": "proposal/parent",
                "step_index": "2",
                "step_count": "2",
            },
        )
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "  ↳ 하위작업 1/2 완료 · 다음: 2/2 남은 하위작업" in briefing.text
    assert "    ├─ [1/2] ~완료된 하위작업~ — ✅ 완료" in briefing.text
    assert "    └─ [2/2] 남은 하위작업 — 승인 대기" in briefing.text


def test_attention_for_blocked_dependent_task_mentions_blocker_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/blocker", "A 세션 종료", due_date=NOW.date()))
    store.save_proposal(
        _proposal(
            "proposal/downstream",
            "B 서버 전환",
            due_date=NOW.date(),
            metadata={"participants": "me", "depends_on_proposal_ids": "proposal/blocker"},
        )
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "*B 서버 전환*: 기한: 2026년 5월 20일(수). 먼저 *A 세션 종료* 완료 여부가 확인되어야 합니다" in briefing.text
    assert "선행 작업의 완료/진행/연기 상태를 알려주세요" in briefing.text


def test_attention_can_see_blocker_outside_personal_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        Proposal(
            proposal_id="proposal/external-blocker",
            source_message_id="slack/DOTHER/source-external-blocker",
            proposer_id="teammate",
            title="A 컴퓨터 세션 종료",
            raw_text="A 컴퓨터 세션 종료",
            kind="task",
            status="approved",
            assigned_to="teammate",
            task_management_area="work",
            discussion_id="private/DOTHER/source-external-blocker",
            message_id="slack/DOTHER/source-external-blocker/1",
            required_approvers=("teammate",),
            approvals=("teammate",),
            due_date=NOW.date(),
            created_at=NOW,
            updated_at=NOW,
            metadata={"participants": "teammate"},
        )
    )
    store.save_proposal(
        _proposal(
            "proposal/personal-downstream",
            "B 메인 서버 전환",
            due_date=NOW.date(),
            metadata={"participants": "me", "depends_on_proposal_ids": "proposal/external-blocker"},
        )
    )

    briefing = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]

    assert "- 2026년 5월 20일(수)까지 A 컴퓨터 세션 종료를 진행하면 됩니다" not in briefing.text
    assert "A 컴퓨터 세션 종료" in briefing.text
    assert "*B 메인 서버 전환*: 기한: 2026년 5월 20일(수). 먼저 *A 컴퓨터 세션 종료* 완료 여부가 확인되어야 합니다" in briefing.text


def test_proactive_checks_ask_about_due_unfinished_work_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/due", "ProjectA 후속 정리", due_date=NOW.date()))
    store.save_proposal(_proposal("proposal/done", "완료된 보고", status="done", due_date=NOW.date()))

    first = build_proactive_checks(store, now=NOW.replace(hour=15), actor_id="me", reserve=True)
    duplicate = build_proactive_checks(store, now=NOW.replace(hour=15, minute=5), actor_id="me", reserve=True)

    assert len(first) == 1
    assert duplicate == ()
    assert first[0].message_type == "due_work_check"
    assert first[0].proposal_id == "proposal/due"
    assert "ProjectA 후속 정리" in first[0].text
    assert "완료" in first[0].text
    assert any(event["type"] == "proactive_check.created" for event in store.read_events())


def test_proactive_checks_skip_deferred_missing_info_until_resolved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/deferred-due",
            "회의 장소 확인",
            due_date=NOW.date(),
            metadata={
                "participants": "me",
                "deferred_until": date(2026, 5, 21).isoformat() + "T08:30:00",
                "deferred_missing_slots": "location",
            },
        )
    )

    assert build_proactive_checks(store, now=NOW.replace(hour=15), actor_id="me", reserve=True) == ()



def test_end_of_day_review_checks_today_open_work_and_ignores_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date()))
    store.save_proposal(
        _proposal(
            "proposal/event",
            "오늘 개인 예약 예약",
            kind="event",
            scheduled_date=NOW.date(),
            time_window="10:00",
        )
    )
    store.save_proposal(
        _proposal(
            "proposal/done",
            "완료된 보고",
            status="done",
            due_date=NOW.date(),
            metadata={"completed_at": NOW.replace(hour=13).isoformat(timespec="seconds")},
        )
    )
    _save_pending(
        store,
        _proposal(
            "proposal/pending",
            "오늘 회의 장소 정하기",
            kind="event",
            status="awaiting_approval",
            scheduled_date=NOW.date(),
            missing_slots=("location",),
        ),
        "approval/pending",
    )

    first = build_end_of_day_review(store, now=NOW.replace(hour=21), actor_id="me", reserve=True)
    duplicate = build_end_of_day_review(store, now=NOW.replace(hour=21, minute=5), actor_id="me", reserve=True)

    assert len(first) == 1
    assert duplicate == ()
    assert first[0].message_type == "end_of_day_review"
    text = first[0].text
    assert "오늘 보고서 정리" in text
    assert "오늘 개인 예약 예약" in text
    assert "오늘 회의 장소 정하기" in text
    assert "완료된 보고" in text
    assert "오늘 남은 항목 확인" in text
    assert "오늘 남은 개인 항목만 정리합니다" in text
    assert "이미 완료 처리된 항목은 다시 묻지 않습니다" in text
    assert "오늘 마감 전 진행 확인" not in text
    assert "퇴근 전 진행 확인" not in text
    assert "이번 7일" not in text
    assert first[0].card["review_count"] == "2"
    assert first[0].card["pending_count"] == "1"
    assert first[0].card["completed_today_count"] == "1"
    assert any(event["type"] == "briefing.end_of_day.created" for event in store.read_events())


def test_end_of_day_review_is_quiet_when_no_open_today_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/done",
            "완료된 보고",
            status="done",
            due_date=NOW.date(),
            metadata={"completed_at": NOW.isoformat(timespec="seconds")},
        )
    )
    store.save_proposal(_proposal("proposal/future", "내일 보고", due_date=date(2026, 5, 21)))

    assert build_end_of_day_review(store, now=NOW.replace(hour=21), actor_id="me", reserve=True) == ()
    assert store.read_events() == ()


def test_end_of_day_review_cli_dry_run_does_not_consume_then_reserve_dedupes(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    store = _store(state_dir)
    store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date()))

    main(
        [
            "--state",
            str(state_dir),
            "end-of-day-review",
            "--now",
            NOW.replace(hour=21).isoformat(timespec="seconds"),
        ]
    )
    dry_output = capsys.readouterr().out
    assert "오늘 남은 항목 확인" in dry_output
    assert "오늘 마감 전 진행 확인" not in dry_output

    real = build_end_of_day_review(_store(state_dir), now=NOW.replace(hour=21, minute=1), actor_id="me", reserve=True)
    duplicate = build_end_of_day_review(
        _store(state_dir),
        now=NOW.replace(hour=21, minute=2),
        actor_id="me",
        reserve=True,
    )
    assert len(real) == 1
    assert duplicate == ()


def test_natural_completion_flows_through_semantic_patch_before_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/packing", "출장 준비물 싸기", due_date=NOW.date()))

    result = TeamTaskOrchestrator(store).handle_message(
        _message("출장 준비물 다 쌌어", ts="1001.000001", at=NOW.replace(hour=20))
    )

    updated = store.get_proposal("proposal/packing")
    assert updated is not None
    assert updated.status == "done"
    assert updated.metadata["completed_by"] == "me"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_completion"
    assert result.outbound_messages[0].message_type == "proposal_completed"
    assert "완료로 표시했습니다" in result.outbound_messages[0].text
    assert any(event["type"] == "proposal.completed" for event in store.read_events())


def test_partial_progress_records_remaining_work_without_completing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/slides", "리뷰위원회 발표자료 제작", due_date=NOW.date()))

    result = TeamTaskOrchestrator(store).handle_message(
        _message("리뷰위원회 발표자료 초안은 반쯤 했고 검토만 남음", ts="1002.000001", at=NOW.replace(hour=11))
    )

    updated = store.get_proposal("proposal/slides")
    assert updated is not None
    assert updated.status == "approved"
    assert updated.metadata["progress_status"] == "partial"
    assert updated.metadata["progress_percent"] == "50"
    assert updated.metadata["remaining_work"] == "검토"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_progress"
    assert result.outbound_messages[0].message_type == "proposal_progress_updated"
    assert "남은 일: 검토" in result.outbound_messages[0].text


def test_progress_update_messages_use_update_scoped_dedupe_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/slides", "심의위원회 발표자료 제작", due_date=NOW.date()))
    first_patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/slides",
        actor_id="me",
        body="first progress update",
        temporal_update={
            "progress_status": "partial",
            "remaining_work": "review",
            "semantic_update_type": "progress",
        },
        reason="progress",
        target_confidence=0.95,
        evidence_text="first progress",
    )
    second_patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/slides",
        actor_id="me",
        body="second progress update",
        temporal_update={
            "progress_status": "partial",
            "remaining_work": "send",
            "semantic_update_type": "progress",
        },
        reason="progress",
        target_confidence=0.95,
        evidence_text="second progress",
    )

    first = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(first_patch)).handle_message(
        _message("first progress", ts="1002.100001", at=NOW.replace(hour=11))
    )
    second = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(second_patch)).handle_message(
        _message("second progress", ts="1002.100002", at=NOW.replace(hour=11, minute=1))
    )

    first_key = first.outbound_messages[0].card["dedupe_key"]
    second_key = second.outbound_messages[0].card["dedupe_key"]
    assert first_key.startswith("slack-outbound/me/proposal_progress_updated/proposal/slides/")
    assert second_key.startswith("slack-outbound/me/proposal_progress_updated/proposal/slides/")
    assert first_key != second_key


def test_progress_patch_can_also_correct_visible_schedule_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/study-group",
            "study-group topic discussion",
            kind="event",
            scheduled_date=NOW.date(),
            time_window="15:00",
            metadata={"participants": "me", "location": "meeting room"},
        )
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/study-group",
        actor_id="me",
        body="reschedule to early afternoon and report after done",
        temporal_update={
            "scheduled_date": "2026-05-20",
            "time_window": "13:00-13:30",
            "progress_status": "scheduled_for_13_00_to_13_30",
            "remaining_work": "report after discussion",
            "semantic_update_type": "correction",
        },
        reason="progress_with_schedule_correction",
        target_confidence=0.95,
        evidence_text="reschedule to early afternoon",
    )

    TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("schedule update", ts="1002.500001", at=NOW.replace(hour=13))
    )

    updated = store.get_proposal("proposal/study-group")
    assert updated is not None
    assert updated.scheduled_date == NOW.date()
    assert updated.time_window == "13:00-13:30"
    assert updated.metadata["progress_status"] == "scheduled_for_13_00_to_13_30"
    assert updated.metadata["remaining_work"] == "report after discussion"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_correction"


def test_multiturn_deferral_progress_check_and_completion_flow(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/kea", "ProjectA 후속 정리", due_date=NOW.date(), time_window="morning"))
    orchestrator = TeamTaskOrchestrator(store)

    deferred = orchestrator.handle_message(
        _message("ProjectA 후속 정리는 금요일 오후로 미뤄줘", ts="1003.000001", at=NOW.replace(hour=9))
    ).proposals[0]
    assert deferred.due_date == date(2026, 5, 22)
    assert deferred.scheduled_date is None
    assert deferred.time_window == "afternoon"
    assert deferred.metadata["last_state_linked_update_type"] == "semantic_deferral"

    progressed = orchestrator.handle_message(
        _message("ProjectA 후속 정리 자료는 반쯤 했고 공유만 남음", ts="1003.000002", at=NOW.replace(hour=14))
    ).proposals[0]
    assert progressed.status == "approved"
    assert progressed.metadata["remaining_work"] == "공유"

    not_yet_due = build_proactive_checks(store, now=NOW.replace(hour=16), actor_id="me", reserve=True)
    due = build_proactive_checks(store, now=datetime(2026, 5, 22, 9), actor_id="me", reserve=True)
    assert not_yet_due == ()
    assert len(due) == 1
    assert "ProjectA 후속 정리" in due[0].text

    completed = orchestrator.handle_message(
        _message("ProjectA 후속 정리 완료", ts="1003.000003", at=datetime(2026, 5, 22, 16))
    ).proposals[0]
    assert completed.status == "done"
    assert build_proactive_checks(store, now=datetime(2026, 5, 22, 17), actor_id="me", reserve=True) == ()

    event_types = [event["type"] for event in store.read_events()]
    assert "proposal.changed" in event_types
    assert "proactive_check.created" in event_types
    assert "proposal.completed" in event_types


def test_completion_like_feedback_without_confident_target_does_not_mutate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/packing", "출장 준비물 싸기", due_date=NOW.date()))
    store.save_proposal(_proposal("proposal/slides", "리뷰위원회 발표자료 제작", due_date=NOW.date()))

    result = TeamTaskOrchestrator(store).handle_message(
        _message("다 했어", ts="1004.000001", at=NOW.replace(hour=18))
    )

    assert result.proposals == ()
    assert {proposal.status for proposal in store.list_proposals()} == {"approved"}
    assert any(
        event["type"] == "agent.patch.rejected"
        and event["payload"]["reason"] == "agent_requested_clarification"
        for event in store.read_events()
    )


def test_completion_feedback_cannot_bypass_pending_approval_or_missing_slots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/babyfair",
            "Example Lab 워크숍 날짜 정하기",
            kind="question",
            status="awaiting_approval",
            missing_slots=("exact_date",),
        ),
        "approval/babyfair",
    )

    TeamTaskOrchestrator(store).handle_message(
        _message("Example Lab 워크숍 날짜 정하기 완료", ts="1004.500001", at=NOW.replace(hour=18))
    )

    pending = store.get_proposal("proposal/babyfair")
    assert pending is not None
    assert pending.status == "awaiting_approval"
    assert pending.missing_slots
    assert pending.metadata.get("completed_at", "") == ""


def test_malformed_semantic_update_type_is_rejected_before_generic_patch_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/packing", "출장 준비물 싸기", due_date=NOW.date()))

    result = TeamTaskOrchestrator(store, operating_agent=MalformedCompletionPatchAgent()).handle_message(
        _message("출장 준비물 다 쌌어", ts="1004.600001", at=NOW.replace(hour=18))
    )

    unchanged = store.get_proposal("proposal/packing")
    assert unchanged is not None
    assert unchanged.status == "approved"
    assert unchanged.metadata.get("completed_at", "") == ""
    assert result.outbound_messages[0].message_type == "agent_patch_rejected"
    assert any(
        event["type"] == "agent.patch.rejected"
        and event["payload"]["reason"] == "completion_requires_done_status"
        for event in store.read_events()
    )


def test_scoped_completion_records_progress_without_closing_pending_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_pending(
        store,
        _proposal(
            "proposal/kickoff",
            "project kickoff meeting",
            kind="event",
            status="awaiting_approval",
            missing_slots=("time",),
            scheduled_date=date(2026, 5, 25),
            metadata={"participants": "me,external", "needs_exact_time": "true"},
        ),
        "approval/kickoff",
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/kickoff",
        actor_id="me",
        body="kickoff preparation is done",
        temporal_update={
            "status": "done",
            "progress_status": "complete",
            "completion_scope": "preparation",
            "semantic_update_type": "completion",
        },
        reason="scoped_completion",
        target_confidence=0.95,
        evidence_text="preparation is done",
    )

    result = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("kickoff preparation is done", ts="1004.700001", at=NOW.replace(hour=18))
    )

    updated = store.get_proposal("proposal/kickoff")
    assert updated is not None
    assert updated.status == "awaiting_approval"
    assert updated.missing_slots == ("time",)
    assert updated.metadata["progress_status"] == "complete"
    assert updated.metadata["completion_scope"] == "preparation"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_progress"
    assert result.outbound_messages[0].message_type == "proposal_progress_updated"
    assert not any(event["type"] == "agent.patch.rejected" for event in store.read_events())


def test_confirmation_patch_updates_slots_without_unsupported_status_rejection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/hospital",
            "hospital appointment",
            kind="event",
            scheduled_date=date(2026, 5, 20),
            time_window="10:00",
            metadata={"participants": "me", "location": "clinic"},
        )
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/hospital",
        actor_id="me",
        body="hospital appointment confirmed",
        temporal_update={
            "status": "confirmed",
            "scheduled_date": "2026-05-20",
            "time_window": "10:00",
            "semantic_update_type": "confirmation",
        },
        reason="confirmation",
        target_confidence=0.95,
        evidence_text="confirmed",
    )

    result = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("hospital appointment confirmed", ts="1004.800001", at=NOW.replace(hour=18))
    )

    updated = store.get_proposal("proposal/hospital")
    assert updated is not None
    assert updated.status == "approved"
    assert updated.scheduled_date == date(2026, 5, 20)
    assert updated.time_window == "10:00"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_confirmation"
    assert result.proposals == (updated,)
    assert not any(event["type"] == "agent.patch.rejected" for event in store.read_events())


def test_correction_patch_updates_title_and_external_counterpart_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/postpartum",
            "가족 일정 연락",
            due_date=NOW.date(),
            time_window="오전",
        )
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/postpartum",
        actor_id="me",
        body="가족 일정 연락을 가족 지원 일정 일정처리 확인으로 정정",
        temporal_update={
            "corrected_title": "가족 지원 일정 일정처리 확인",
            "external_owner": "심채현",
            "external_participants": "심채현",
            "participant_label": "심채현",
            "participants": "me",
            "location_optional": "true",
            "semantic_update_type": "correction",
        },
        reason="semantic_correction",
        target_confidence=0.95,
        evidence_text="explicit correction",
    )

    result = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("postpartum correction", ts="1004.850001", at=NOW.replace(hour=18))
    )

    updated = store.get_proposal("proposal/postpartum")
    assert updated is not None
    assert updated.status == "approved"
    assert updated.title == "가족 지원 일정 일정처리 확인"
    assert updated.due_date == NOW.date()
    assert updated.time_window == "오전"
    assert updated.metadata["previous_title"] == "가족 일정 연락"
    assert updated.metadata["external_owner"] == "심채현"
    assert updated.metadata["external_participants"] == "심채현"
    assert updated.metadata["participant_label"] == "심채현"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_correction"
    assert result.outbound_messages[0].message_type == "semantic_patch_applied"
    assert not any(event["type"] == "agent.patch.rejected" for event in store.read_events())


def test_correction_patch_with_remaining_missing_slot_reasks_user(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/study-group",
            "제3회 study-group 주제 논의",
            kind="event",
            scheduled_date=NOW.date(),
            time_window="15:00",
            metadata={
                "external_participants": "박연구 박사님",
                "participant_label": "박연구 박사님",
                "location": "회의실 A",
                "location_optional": "false",
            },
        )
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/study-group",
        actor_id="me",
        body="오전으로 바뀌었는데 정확한 시간은 아직",
        temporal_update={
            "time_window": "오전",
            "needs_exact_time": "true",
            "semantic_update_type": "correction",
        },
        reason="semantic_correction",
        target_confidence=0.95,
        evidence_text="explicit correction with broad time",
    )

    result = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("study-group 오전으로 바뀌었는데 정확한 시간은 아직", ts="1004.860001", at=NOW.replace(hour=18))
    )

    updated = store.get_proposal("proposal/study-group")
    assert updated is not None
    assert updated.status == "awaiting_approval"
    assert updated.missing_slots == ("time",)
    assert result.approval_requests[0].proposal_id == "proposal/study-group"
    assert result.outbound_messages[0].message_type == "approval_request"
    assert "정확한 시간" in result.outbound_messages[0].text


def test_partial_feedback_reasks_with_task_context_and_human_slot_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = _proposal(
        "proposal/study-group",
        "제3회 study-group 주제 논의",
        kind="event",
        status="awaiting_approval",
        scheduled_date=NOW.date(),
        time_window="오전",
        missing_slots=("time",),
        metadata={
            "needs_exact_time": "true",
            "location_optional": "true",
        },
    )
    _save_pending(store, proposal, "approval/ai")

    result = TeamTaskOrchestrator(store).handle_feedback(
        request_id="approval/ai",
        actor_id="me",
        body="오전쯤이고 정확한 시간은 아직",
        changed_at=NOW.replace(hour=18),
        temporal_update={
            "time_window": "오전",
            "needs_exact_time": "true",
            "semantic_update_type": "correction",
        },
    )

    message = result.outbound_messages[0]
    assert message.message_type == "missing_info_followup"
    assert "제3회 study-group 주제 논의" in message.text
    assert "정확한 시간" in message.text
    assert "변경 approval/ai" in message.text
    assert "아직 정보가 더 필요합니다" not in message.text
    assert "time" not in message.text
    assert message.card["dedupe_key"].startswith("slack-missing-info-followup/approval/ai/")


def test_completion_patch_applies_date_time_correction_before_closing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/fund",
            "펀드 해지",
            due_date=date(2026, 5, 21),
            time_window="오전 10시 전까지",
        )
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/fund",
        actor_id="me",
        body="펀드 해지 완료, 실제 일정은 오늘 오전 10시",
        temporal_update={
            "status": "done",
            "due_date": "2026-05-20",
            "time_window": "오전 10시",
            "participants": "me",
            "semantic_update_type": "completion",
        },
        reason="completion_and_date_time_correction",
        target_confidence=0.96,
        evidence_text="completion plus corrected date",
    )

    result = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("fund completion correction", ts="1004.860001", at=NOW.replace(hour=18))
    )

    updated = store.get_proposal("proposal/fund")
    assert updated is not None
    assert updated.status == "done"
    assert updated.due_date == date(2026, 5, 20)
    assert updated.time_window == "오전 10시"
    assert updated.metadata["completed_by"] == "me"
    assert updated.metadata["last_state_linked_update_type"] == "semantic_completion"
    assert result.outbound_messages[0].message_type == "proposal_completed"
    assert not any(event["type"] == "agent.patch.rejected" for event in store.read_events())


def test_secretary_slack_send_failure_does_not_consume_delivery_dedupe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date()))
    message = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]
    dedupe_key = message.card["dedupe_key"]
    adapter = SlackDmAdapter(
        SlackDmConfig(actor_id="me", dm_channel_id="DTEST"),
        FailingSlackClient(),
    )

    with pytest.raises(SlackAdapterError, match="transient Slack failure"):
        dispatch_slack_outbound(store, adapter, (message,), sent_at=NOW)

    assert store.has_outbound_delivery(dedupe_key) is False
    assert any(
        event["type"] == "slack.message.failed"
        and event["payload"]["dedupe_key"] == dedupe_key
        for event in store.read_events()
    )


def test_secretary_slack_send_failure_is_retryable_with_same_dedupe_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date()))
    message = build_morning_briefing(store, now=NOW, actor_id="me", reserve=False)[0]
    dedupe_key = message.card["dedupe_key"]

    with pytest.raises(SlackAdapterError):
        dispatch_slack_outbound(
            store,
            SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), FailingSlackClient()),
            (message,),
            sent_at=NOW,
        )

    success_client = SuccessfulSlackClient()
    dispatch_slack_outbound(
        store,
        SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), success_client),
        (message,),
        sent_at=NOW.replace(minute=5),
    )

    assert success_client.sent == [("DTEST", "3000.000001", message.text)]
    assert store.has_outbound_delivery(dedupe_key) is True
    sent_events = [event for event in store.read_events() if event["type"] == "slack.message.sent"]
    assert sent_events[-1]["payload"]["dedupe_key"] == dedupe_key


def test_secretary_cli_dry_run_does_not_consume_then_send_dedupes(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    store = _store(state_dir)
    store.save_proposal(_proposal("proposal/due", "오늘 보고서 정리", due_date=NOW.date()))

    main(
        [
            "--state",
            str(state_dir),
            "morning-briefing",
            "--now",
            NOW.isoformat(timespec="seconds"),
            "--dashboard-url",
            "http://127.0.0.1:8787/dashboard.html",
        ]
    )
    dry_output = capsys.readouterr().out
    assert "오늘 아침 브리핑" in dry_output

    real = build_morning_briefing(_store(state_dir), now=NOW.replace(minute=1), actor_id="me", reserve=True)
    duplicate = build_morning_briefing(_store(state_dir), now=NOW.replace(minute=2), actor_id="me", reserve=True)
    assert len(real) == 1
    assert duplicate == ()


# --- Approval-state-machine regressions (A5/A6/A7/A8) -----------------------


def test_change_with_remaining_missing_slot_keeps_request_pending(tmp_path: Path) -> None:
    # Bug A5: 변경 that fills only the date on a proposal still missing
    # participants must NOT auto-accept/approve; it must keep the request pending
    # and re-question the missing slot.
    store = _store(tmp_path)
    proposal = _proposal(
        "proposal/kickoff",
        "킥오프 회의 잡기",
        kind="event",
        status="awaiting_approval",
        missing_slots=("date", "participants"),
        metadata={
            "needs_exact_time": "false",
            "location_optional": "true",
        },
    )
    # No participant metadata yet, so the participants slot is genuinely open.
    proposal = replace(proposal, metadata={k: v for k, v in proposal.metadata.items() if k != "participants"})
    _save_pending(store, proposal, "approval/kickoff")

    result = TeamTaskOrchestrator(store).handle_change(
        target_id="approval/kickoff",
        actor_id="me",
        body="다음 주 금요일로 잡자",
        changed_at=NOW.replace(hour=11),
    )

    updated = store.get_proposal("proposal/kickoff")
    assert updated is not None
    assert updated.status == "awaiting_approval"
    assert "participants" in updated.missing_slots
    request = store.get_approval_request("approval/kickoff")
    assert request is not None and request.status == "pending"
    assert result.outbound_messages[0].message_type == "missing_info_followup"
    assert "proposal.approved" not in [event["type"] for event in store.read_events()]


def test_change_on_conflict_hold_preserves_conflict_slot(tmp_path: Path) -> None:
    # Bug A6: a proposal held for a scheduling conflict must keep the
    # conflict_resolution slot when a later 변경 only adjusts the date, and must
    # not reach approved until a conflict decision is recorded.
    store = _store(tmp_path)
    existing = _proposal(
        "proposal/existing-meeting",
        "기존 팀 회의",
        kind="event",
        status="approved",
        scheduled_date=date(2026, 5, 22),
    )
    store.save_proposal(existing)
    held = _proposal(
        "proposal/trip",
        "출장 일정",
        kind="question",
        status="awaiting_approval",
        scheduled_date=date(2026, 5, 22),
        missing_slots=("conflict_resolution",),
        metadata={
            "conflict_detected": "true",
            "conflict_with_proposal_ids": "proposal/existing-meeting",
        },
    )
    _save_pending(store, held, "approval/trip")

    result = TeamTaskOrchestrator(store).handle_change(
        target_id="approval/trip",
        actor_id="me",
        body="다음 주 금요일로 미루자",
        changed_at=NOW.replace(hour=11),
    )

    updated = store.get_proposal("proposal/trip")
    assert updated is not None
    assert updated.status == "awaiting_approval"
    assert "conflict_resolution" in updated.missing_slots
    assert "conflict_resolution_action" not in updated.metadata
    assert result.outbound_messages[0].message_type == "missing_info_followup"
    assert "proposal.approved" not in [event["type"] for event in store.read_events()]

    # Once a conflict decision is recorded, the slot clears normally.
    resolved = apply_conflict_resolution_feedback(
        store,
        _message("기존 회의 취소", at=NOW.replace(hour=12)),
        now=NOW.replace(hour=12),
    )
    assert resolved is not None
    assert "conflict_resolution" not in resolved.missing_slots
    assert resolved.metadata.get("conflict_resolution_action") == "cancel_existing"


def test_rejected_missing_slot_request_is_not_resurrected(tmp_path: Path) -> None:
    # Bug A7: the deterministic request_id plus an unguarded upsert used to flip a
    # terminal (rejected) request back to pending when the proposal re-entered a
    # needs-info state. The terminal decision must be preserved.
    store = _store(tmp_path)
    proposal = _proposal(
        "proposal/needs-info",
        "정보 필요 항목",
        kind="event",
        status="awaiting_approval",
        scheduled_date=NOW.date(),
        missing_slots=("participants",),
        metadata={"location_optional": "true", "needs_exact_time": "false"},
    )
    proposal = replace(proposal, metadata={k: v for k, v in proposal.metadata.items() if k != "participants"})
    store.save_proposal(proposal)

    request = approval_request(proposal.proposal_id, "me", now=NOW)
    store.save_approval_request(request)
    # The approver rejects the missing-slot request -> terminal state.
    rejected = TeamTaskOrchestrator(store).handle_approval(
        request_id=request.request_id,
        approver_id="me",
        accepted=False,
        decided_at=NOW.replace(hour=9),
    )
    assert rejected.proposals[0].status == "rejected"
    assert store.get_approval_request(request.request_id).status == "rejected"  # type: ignore[union-attr]

    # Drive the proposal back into a pending-needs-info state and rebuild the
    # missing-slot request: the same deterministic id must NOT revert to pending.
    revived = replace(
        proposal,
        status="awaiting_approval",
        missing_slots=("participants",),
        updated_at=NOW.replace(hour=10),
    )
    store.save_proposal(revived)
    rebuilt, created = TeamTaskOrchestrator(store)._ensure_missing_slot_request(
        revived, actor_id="me", now=NOW.replace(hour=10)
    )

    assert created is False
    assert rebuilt.status == "rejected"
    assert store.get_approval_request(request.request_id).status == "rejected"  # type: ignore[union-attr]

    # A direct re-save of the terminal id at pending status is also rejected by
    # the store-level guard.
    store.save_approval_request(replace(request, status="pending", decided_at=None))
    assert store.get_approval_request(request.request_id).status == "rejected"  # type: ignore[union-attr]


def test_direct_semantic_deferral_keeps_approved_proposal(tmp_path: Path) -> None:
    # Bug A8: deferring a slot on an already-approved proposal via a direct
    # semantic patch must keep the approval (invariant 4 IFF) and acknowledge the
    # deferral instead of re-asking the deferred slot.
    store = _store(tmp_path)
    store.save_proposal(
        _proposal(
            "proposal/event",
            "팀 워크숍",
            kind="event",
            status="approved",
            scheduled_date=date(2026, 5, 22),
            time_window="14:00",
            metadata={"location_optional": "true"},
        )
    )
    patch = ProposalPatch(
        request_id="",
        proposal_id="proposal/event",
        actor_id="me",
        body="다음 주 금요일로 미루고 시간은 아직 미정",
        temporal_update={
            "scheduled_date": "2026-05-29",
            "time_window": "미정",
            "defer_missing_slots": "time",
            "needs_exact_time": "true",
            "semantic_update_type": "deferral",
        },
        reason="defer_time",
        target_confidence=0.95,
        evidence_text="다음 주 금요일로 미루고 시간은 아직 미정",
    )

    result = TeamTaskOrchestrator(store, operating_agent=DirectPatchAgent(patch)).handle_message(
        _message("워크숍 다음 주 금요일로 미루고 시간은 아직 미정", ts="1009.000001", at=NOW.replace(hour=9))
    )

    updated = store.get_proposal("proposal/event")
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approvals == ("me",)
    assert updated.scheduled_date == date(2026, 5, 29)
    message_types = {message.message_type for message in result.outbound_messages}
    assert "missing_info_deferred" in message_types
    assert "approval_request" not in message_types
    assert "missing_info_followup" not in message_types

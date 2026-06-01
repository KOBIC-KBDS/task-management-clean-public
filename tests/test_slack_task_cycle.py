from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import OperatingAgentDecision
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.slack_adapter import FakeSlackWebClient, SlackDmAdapter, SlackDmConfig
from task_management.slack_cycle import run_slack_task_cycle
from task_management.slack_digest import build_today_update_digest
from task_management.store import TeamTaskStore
from task_management.task_reconciler import reconcile_message


NOW = datetime(2026, 5, 18, 9, 0, 0)


class NoActionAgent:
    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        return OperatingAgentDecision(
            action="no_action",
            source="test_no_action_agent",
            confidence=1.0,
            rationale="Force reconciliation to prove state-linked task recovery.",
        )


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def _committee_event() -> Proposal:
    return Proposal(
        proposal_id="codex/slack/DTEST/1779026714.216899/1",
        source_message_id="slack/DTEST/1779026714.216899",
        proposer_id="me",
        title="리뷰위원회 발표 배석",
        raw_text="내일 오후 4시 반 리뷰위원회 발표 배석. 참석자 나/김센터 센터장님/이협업 선생님",
        kind="event",
        status="approved",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/DTEST/slack/DTEST/1779026714.216899",
        message_id="slack/DTEST/1779026714.216899",
        required_approvers=("me",),
        approvals=("me",),
        scheduled_date=date(2026, 5, 18),
        time_window="16:30",
        created_at=datetime(2026, 5, 17, 23, 5, 14),
        updated_at=datetime(2026, 5, 17, 23, 5, 14),
        metadata={
            "participants": "me",
            "external_participants": "김센터 센터장님, 이협업 선생님",
            "participant_label": "나/김센터 센터장님/이협업 선생님",
            "location_optional": "true",
        },
    )


def _slack_message(ts: str, text: str, *, at: datetime = NOW) -> dict[str, str]:
    return {
        "channel": "DTEST",
        "ts": ts,
        "user": "UUSER",
        "text": text,
        "received_at": at.isoformat(timespec="seconds"),
    }


def test_reconciler_recovers_presentation_materials_prep_from_existing_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_committee_event())
    message = IncomingMessage(
        message_id="slack/DTEST/1779035350.608929",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text="오후 4시 반 리뷰위원회 발표자료를 아침에 제작해야함.",
        received_at=datetime(2026, 5, 18, 1, 29, 10),
    )

    created = reconcile_message(store, message, reconciled_at=NOW)
    duplicate = reconcile_message(store, message, reconciled_at=NOW.replace(minute=1))

    assert len(created) == 1
    assert duplicate == ()
    prep = created[0]
    assert prep.title == "리뷰위원회 발표자료 제작"
    assert prep.kind == "task"
    assert prep.status == "approved"
    assert prep.due_date == date(2026, 5, 18)
    assert prep.time_window == "morning"
    assert prep.metadata["parent_proposal_id"] == "codex/slack/DTEST/1779026714.216899/1"
    assert prep.metadata["link_type"] == "prep_subtask"
    assert prep.metadata["source_provider"] == "slack"
    assert len([item for item in store.list_proposals() if item.metadata.get("link_type") == "prep_subtask"]) == 1
    assert any(event["type"] == "slack.message.reconciled" for event in store.read_events())


def test_orchestrator_updates_existing_event_from_official_schedule_block(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_committee_event())
    message = IncomingMessage(
        message_id="slack/DTEST/1779063354.918199",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text=(
            "예시 데이터 수집·저장 인프라 심의일정\n"
            "대전 서구 둔산중로134번길 13 토요코인호텔 KW컨벤션 2층 컨퍼런스D홀\n"
            "발표일자\n"
            "2026/05/18 17:00"
        ),
        received_at=datetime(2026, 5, 18, 9, 15, 54),
    )

    result = TeamTaskOrchestrator(store, operating_agent=NoActionAgent()).handle_message(message)

    assert len(result.proposals) == 1
    assert len(store.list_proposals()) == 1
    updated = store.get_proposal("codex/slack/DTEST/1779026714.216899/1")
    assert updated is not None
    assert updated.title == "예시 데이터 수집·저장 인프라 심의 발표 배석"
    assert updated.status == "approved"
    assert updated.scheduled_date == date(2026, 5, 18)
    assert updated.time_window == "17:00"
    assert updated.metadata["location"] == "대전 서구 둔산중로134번길 13 토요코인호텔 KW컨벤션 2층 컨퍼런스D홀"
    assert updated.metadata["official_title"] == "예시 데이터 수집·저장 인프라 심의"
    assert updated.metadata["last_schedule_update_message_id"] == "slack/DTEST/1779063354.918199"
    assert "location_optional" not in updated.metadata
    event_types = [event["type"] for event in store.read_events()]
    assert "proposal.changed" in event_types
    assert "slack.message.reconciled" in event_types
    assert "agent.decision.created" in event_types


def test_slack_task_cycle_polls_reconciles_renders_and_sends_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_committee_event())
    client = FakeSlackWebClient(
        messages=[
            _slack_message(
                "1779035350.608929",
                "오후 4시 반 리뷰위원회 발표자료를 아침에 제작해야함.",
                at=datetime(2026, 5, 18, 1, 29, 10),
            )
        ],
        channel_id="DTEST",
    )
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client)

    result = run_slack_task_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=NoActionAgent()),
        adapter=adapter,
        now=NOW,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        monthly_page_output=tmp_path / "out" / "slack-page.md",
        digest_output=tmp_path / "out" / "digest.md",
        month=date(2026, 5, 1),
        actor_id="me",
        dashboard_url="http://127.0.0.1:8787/dashboard.html",
        canvas_url="https://slack.example/canvas",
        send=True,
    )

    assert len(result.messages) == 1
    assert len(result.reconciled_proposals) == 1
    assert store.get_integration_state("slack.dm.me.last_ts") == "1779035350.608929"
    assert len(client.sent) == 1
    assert "리뷰위원회 발표자료 제작" in client.sent[0][2]
    assert "https://slack.example/canvas" in client.sent[0][2]
    assert "리뷰위원회 발표자료 제작" in (tmp_path / "out" / "dashboard.html").read_text(encoding="utf-8")
    assert "리뷰위원회 발표자료 제작" in (tmp_path / "out" / "slack-page.md").read_text(encoding="utf-8")
    assert "오늘 Task 업데이트" in (tmp_path / "out" / "digest.md").read_text(encoding="utf-8")

    duplicate = run_slack_task_cycle(
        store=store,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=NoActionAgent()),
        adapter=SlackDmAdapter(
            SlackDmConfig(actor_id="me", dm_channel_id="DTEST"),
            client,
            oldest=store.get_integration_state("slack.dm.me.last_ts") or "",
        ),
        now=NOW.replace(minute=5),
        dashboard_output=tmp_path / "out" / "dashboard.html",
        monthly_page_output=tmp_path / "out" / "slack-page.md",
        digest_output=tmp_path / "out" / "digest.md",
        month=date(2026, 5, 1),
        actor_id="me",
        dashboard_url="http://127.0.0.1:8787/dashboard.html",
        canvas_url="https://slack.example/canvas",
        send=True,
    )
    assert duplicate.messages == ()
    assert len([item for item in store.list_proposals() if item.metadata.get("link_type") == "prep_subtask"]) == 1
    assert len(client.sent) == 1


def test_today_digest_surfaces_floating_items_and_missing_slots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_proposal(_committee_event())
    store.save_proposal(
        Proposal(
            proposal_id="proposal/floating",
            source_message_id="slack/DTEST/1779035459.034629",
            proposer_id="me",
            title="ProjectA 진행상황 follow-up",
            raw_text="ProjectA 진행상황도 Follow up 해야함.",
            kind="question",
            status="awaiting_approval",
            assigned_to="me",
            task_management_area="work",
            discussion_id="private/DTEST/slack/DTEST/1779035459.034629",
            message_id="slack/DTEST/1779035459.034629",
            required_approvers=("me",),
            missing_slots=("date",),
            created_at=NOW,
            updated_at=NOW,
            metadata={"materials": "ProjectA"},
        )
    )

    digest = build_today_update_digest(
        store,
        now=NOW,
        dashboard_url="http://127.0.0.1:8787/dashboard.html",
    )

    assert "리뷰위원회 발표 배석" in digest
    assert "ProjectA 진행상황 follow-up" in digest
    assert "담당자는 사용자님으로 잡혀 있지만 아직 정해지지 않은 정보가 있습니다: *일시*" in digest
    assert "*일시* 정보를 알려주세요" in digest
    assert "웹 task page" in digest

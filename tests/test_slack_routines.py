from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from task_management.domain import IncomingMessage, OutboundMessage
from task_management.frontend import build_web_task_page_model
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.reminders import build_due_reminders
from task_management.slack_adapter import (
    FakeSlackWebClient,
    SlackAdapterError,
    SlackDmAdapter,
    SlackDmConfig,
    diagnose_slack_live_config,
    dispatch_slack_outbound,
    run_slack_dm_once,
)
from task_management.store import TeamTaskStore
from task_management.task_core_bridge import build_task_management_task_export_from_proposals, validate_with_task_core


NOW = datetime(2026, 5, 5, 10, 0, 0)


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def _slack_message(ts: str, text: str, *, user: str = "UUSER", at: datetime = NOW) -> dict[str, str]:
    return {
        "channel": "DTEST",
        "ts": ts,
        "user": user,
        "text": text,
        "received_at": at.isoformat(timespec="seconds"),
    }


def _run_slack(root: Path, client: FakeSlackWebClient, *, send: bool = False):
    store = _store(root)
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST", bot_user_id="UBOT")
    adapter = SlackDmAdapter(
        config,
        client,
        oldest=store.get_integration_state("slack.dm.me.last_ts") or "",
    )
    return run_slack_dm_once(
        store=store,
        orchestrator=TeamTaskOrchestrator(store),
        adapter=adapter,
        send=send,
        now=NOW,
    )


def test_slack_dm_vague_event_intake_creates_date_window_question_and_dedupes(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[
            _slack_message("1000.000001", "워크숍 이번주 목~일 중 하루 가야함"),
            _slack_message("1000.000002", "봇 메시지는 무시", user="UBOT"),
        ],
        channel_id="DTEST",
    )

    result = _run_slack(tmp_path, client)
    store = _store(tmp_path)
    proposal = store.list_proposals()[0]

    assert result.messages[0].message_id == "slack/DTEST/1000.000001"
    assert proposal.kind == "question"
    assert proposal.status == "awaiting_approval"
    assert proposal.metadata["date_window_start"] == "2026-05-07"
    assert proposal.metadata["date_window_end"] == "2026-05-10"
    assert set(proposal.missing_slots) >= {"exact_date", "participants"}
    assert result.outbound_messages[0].surface == "personal_chat"
    assert result.outbound_messages[0].recipient_id == "me"

    duplicate = _run_slack(tmp_path, client)
    assert duplicate.messages == ()
    assert duplicate.results == ()
    assert len(store.list_proposals()) == 1
    assert store.get_integration_state("slack.dm.me.last_ts") == "1000.000001"


def test_slack_feedback_confirms_proposal_web_triage_and_task_core_preview(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[_slack_message("1000.000001", "워크숍 이번주 목~일 중 하루 가야함")],
        channel_id="DTEST",
    )
    _run_slack(tmp_path, client)
    client.messages.append(
        _slack_message(
            "1001.000001",
            "토요일 오전에 나랑 팀원 같이 갈게",
            at=NOW.replace(hour=10, minute=5),
        )
    )

    result = _run_slack(tmp_path, client)
    store = _store(tmp_path)
    proposal = [item for item in result.results if item.proposals][-1].proposals[0]

    assert proposal.status == "approved"
    assert proposal.kind == "event"
    assert proposal.scheduled_date == date(2026, 5, 9)
    assert proposal.time_window == "morning"
    assert proposal.metadata["participants"] == "me,teammate"

    model = build_web_task_page_model(store, today=NOW.date())
    assert model["sections"]["questions"] == []
    assert any(item["proposal_id"] == proposal.proposal_id for item in model["sections"]["this_week"])

    payload = build_task_management_task_export_from_proposals(store.list_proposals(), exported_at=NOW)
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["metadata"]["source_message_id"] == "slack/DTEST/1000.000001"
    assert item["metadata"]["source_provider"] == "slack"
    assert item["metadata"]["source_channel"] == "DTEST"
    assert item["metadata"]["source_ts"] == "1000.000001"
    assert item["metadata"]["participants"] == "me,teammate"
    preview = validate_with_task_core(payload, root=tmp_path / "task-core-root")
    assert preview["ok"] is True
    assert preview["restores"] is False
    assert payload["diagnostics"]["mutates_files"] is False


def test_weekly_single_cell_db_meeting_routine_captures_required_slots(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[
            _slack_message(
                "1002.000001",
                "sample-data sync 미팅 주 1회 화요일 10시 3층 회의실, 나랑 팀원 참석, 자료 준비 필요",
            )
        ],
        channel_id="DTEST",
    )

    result = _run_slack(tmp_path, client)
    routine = result.results[0].proposals[0]

    assert routine.kind == "routine"
    assert routine.metadata["recurrence_frequency"] == "weekly"
    assert routine.metadata["recurrence_weekday"] == "1"
    assert routine.metadata["next_occurrence_date"] == "2026-05-05"
    assert routine.time_window == "10:00"
    assert routine.metadata["location"] == "3층 회의실"
    assert routine.metadata["participants"] == "me,teammate"
    assert routine.metadata["needs_prep"] == "true"
    assert routine.missing_slots == ()
    assert routine.status == "awaiting_approval"
    assert result.results[0].approval_requests[0].approver_id == "teammate"


def test_routine_approval_generates_linked_preparation_subtask(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[
            _slack_message(
                "1003.000001",
                "sample-data sync 미팅 주 1회 화요일 10시 3층 회의실, 나랑 팀원 참석, 자료 준비 필요",
            )
        ],
        channel_id="DTEST",
    )
    created = _run_slack(tmp_path, client).results[0]
    request_id = created.approval_requests[0].request_id
    store = _store(tmp_path)

    approved = TeamTaskOrchestrator(store).handle_approval(
        request_id=request_id,
        approver_id="teammate",
        accepted=True,
        decided_at=NOW.replace(hour=10, minute=10),
    )
    prep = next(item for item in approved.proposals if item.metadata.get("link_type") == "prep_subtask")

    assert prep.kind == "task"
    assert prep.status == "approved"
    assert prep.metadata["parent_proposal_id"] == created.proposals[0].proposal_id
    assert prep.metadata["routine_occurrence_date"] == "2026-05-05"
    assert prep.due_date == date(2026, 5, 4)

    model = build_web_task_page_model(store, today=NOW.date())
    assert any(item["proposal_id"] == prep.proposal_id for item in model["sections"]["prep_subtasks"])
    payload = build_task_management_task_export_from_proposals(store.list_proposals(), exported_at=NOW)
    assert any(item["metadata"].get("parent_proposal_id") == created.proposals[0].proposal_id for item in payload["items"])


def test_day_of_routine_reminder_dm_is_idempotent_and_mentions_outstanding_prep(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[
            _slack_message(
                "1004.000001",
                "sample-data sync 미팅 주 1회 화요일 10시 3층 회의실, 나랑 팀원 참석, 자료 준비 필요",
            )
        ],
        channel_id="DTEST",
    )
    created = _run_slack(tmp_path, client).results[0]
    store = _store(tmp_path)
    TeamTaskOrchestrator(store).handle_approval(
        request_id=created.approval_requests[0].request_id,
        approver_id="teammate",
        accepted=True,
        decided_at=NOW.replace(hour=10, minute=10),
    )

    first = build_due_reminders(store, now=NOW.replace(hour=8), actor_id="me")
    second = build_due_reminders(store, now=NOW.replace(hour=8, minute=1), actor_id="me")

    assert len(first) == 1
    assert second == ()
    assert "10:00" in first[0].text
    assert "3층 회의실" in first[0].text
    assert "사용자님, 팀원님" in first[0].text
    assert "자료 준비" in first[0].text
    assert any(event["type"] == "reminder.created" for event in store.read_events())


def test_pending_missing_info_reminder_is_human_friendly_and_periodic(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[_slack_message("1004.500001", "워크숍 이번주 목~일 중 하루 가야함")],
        channel_id="DTEST",
    )
    _run_slack(tmp_path, client)
    store = _store(tmp_path)

    first = build_due_reminders(store, now=NOW.replace(hour=8), actor_id="me")
    duplicate_same_bucket = build_due_reminders(store, now=NOW.replace(hour=8, minute=30), actor_id="me")
    next_bucket = build_due_reminders(store, now=NOW.replace(hour=14), actor_id="me")

    assert len(first) == 1
    assert first[0].message_type == "missing_info_reminder"
    assert "워크숍" in first[0].text
    assert "담당자는 팀 공동으로 잡혀 있지만" in first[0].text
    assert "*정확한 날짜*, *참여자*" in first[0].text
    assert "변경 approval/" in first[0].text
    assert duplicate_same_bucket == ()
    assert len(next_bucket) == 1


def test_not_decided_feedback_defers_missing_slot_reminders_until_event_day(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[_slack_message("1004.600001", "수요일 점심회식 참석자 나", at=datetime(2026, 5, 18, 10))],
        channel_id="DTEST",
    )
    created = _run_slack(tmp_path, client).results[0]
    store = _store(tmp_path)
    proposal = created.proposals[0]

    assert proposal.scheduled_date == date(2026, 5, 20)
    assert proposal.time_window == "lunch"
    assert proposal.missing_slots == ("time", "location")

    client.messages.append(
        _slack_message(
            "1004.600002",
            "회식장소는 필요하지 않고, 나중에 정해지면 알려줄게. 수요일 점심회식 시간대도 나중에 정해지면 알려줄게.",
            at=datetime(2026, 5, 18, 10, 20),
        )
    )
    deferred = _run_slack(tmp_path, client).results[0].proposals[0]

    assert deferred.missing_slots == ("time",)
    assert deferred.metadata["location_optional"] == "true"
    assert deferred.metadata["deferred_missing_slots"] == "time"
    assert deferred.metadata["deferred_until"] == "2026-05-20T08:30:00"

    before_due = build_due_reminders(store, now=datetime(2026, 5, 19, 8, 30), actor_id="me")
    first_due = build_due_reminders(store, now=datetime(2026, 5, 20, 8, 30), actor_id="me")
    duplicate_same_bucket = build_due_reminders(store, now=datetime(2026, 5, 20, 9, 30), actor_id="me")
    next_bucket = build_due_reminders(store, now=datetime(2026, 5, 20, 10, 30), actor_id="me")

    assert before_due == ()
    assert len(first_due) == 1
    assert "*정확한 시간*" in first_due[0].text
    assert "점심 시간대" in first_due[0].text
    assert duplicate_same_bucket == ()
    assert len(next_bucket) == 1


def test_exact_time_and_location_feedback_resolves_deferred_lunch_event(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[_slack_message("1004.700001", "수요일 점심회식 참석자 나", at=datetime(2026, 5, 18, 10))],
        channel_id="DTEST",
    )
    created = _run_slack(tmp_path, client).results[0]
    store = _store(tmp_path)
    request_id = created.approval_requests[0].request_id
    client.messages.append(
        _slack_message(
            "1004.700002",
            "회식장소는 필요하지 않고, 나중에 정해지면 알려줄게. 수요일 점심회식 시간대도 나중에 정해지면 알려줄게.",
            at=datetime(2026, 5, 18, 10, 20),
        )
    )
    _run_slack(tmp_path, client)

    client.messages.append(
        _slack_message(
            "1004.700003",
            "수요일 점심회식: 오전 11시 30분 편백연가 도룡점",
            at=datetime(2026, 5, 18, 10, 29),
        )
    )
    result = _run_slack(tmp_path, client)
    resolved = result.results[0].proposals[0]

    assert resolved.status == "approved"
    assert resolved.kind == "event"
    assert resolved.missing_slots == ()
    assert resolved.scheduled_date == date(2026, 5, 20)
    assert resolved.time_window == "11:30"
    assert resolved.metadata["location"] == "편백연가 도룡점"
    assert "deferred_until" not in resolved.metadata
    assert store.get_approval_request(request_id).status == "accepted"  # type: ignore[union-attr]
    assert result.results[0].outbound_messages[0].message_type == "proposal_approved"
    reminders = build_due_reminders(store, now=datetime(2026, 5, 20, 8, 30), actor_id="me")
    assert len(reminders) == 1
    assert reminders[0].message_type == "event_reminder"
    assert "11:30" in reminders[0].text
    assert "편백연가 도룡점" in reminders[0].text


def test_reminder_dry_run_does_not_consume_delivery_dedupe(tmp_path: Path) -> None:
    client = FakeSlackWebClient(
        messages=[
            _slack_message(
                "1005.000001",
                "sample-data sync 미팅 주 1회 화요일 10시 3층 회의실, 나랑 팀원 참석, 자료 준비 필요",
            )
        ],
        channel_id="DTEST",
    )
    created = _run_slack(tmp_path, client).results[0]
    store = _store(tmp_path)
    TeamTaskOrchestrator(store).handle_approval(
        request_id=created.approval_requests[0].request_id,
        approver_id="teammate",
        accepted=True,
        decided_at=NOW.replace(hour=10, minute=10),
    )

    dry_run = build_due_reminders(store, now=NOW.replace(hour=8), actor_id="me", reserve=False)
    real_run = build_due_reminders(store, now=NOW.replace(hour=8, minute=1), actor_id="me", reserve=True)
    duplicate = build_due_reminders(store, now=NOW.replace(hour=8, minute=2), actor_id="me", reserve=True)

    assert len(dry_run) == 1
    assert len(real_run) == 1
    assert duplicate == ()


def test_slack_outbound_delivery_records_provider_message_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(channel_id="DTEST")
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client)
    message = _run_slack(
        tmp_path,
        FakeSlackWebClient(
            messages=[_slack_message("1006.000001", "워크숍 이번주 목~일 중 하루 가야함")],
            channel_id="DTEST",
        ),
    ).outbound_messages[0]

    dispatch_slack_outbound(store, adapter, (message,), sent_at=NOW)

    assert client.sent[0][1] == "2000.000001"
    assert store.has_outbound_delivery(f"slack-outbound/me/approval_request/{message.approval_request_id}")


def test_slack_outbound_skips_recipients_outside_configured_personal_dm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(channel_id="DTEST")
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="DTEST"), client)
    teammate_message = OutboundMessage(
        surface="personal_chat",
        recipient_id="teammate",
        message_type="approval_request",
        text="팀원 승인 요청",
        proposal_id="proposal-1",
        approval_request_id="approval-1",
    )

    dispatch_slack_outbound(store, adapter, (teammate_message,), sent_at=NOW)

    assert client.sent == []
    assert any(
        event["type"] == "slack.message.skipped"
        and event["payload"]["reason"] == "unsupported_recipient"
        and event["payload"]["recipient_id"] == "teammate"
        for event in store.read_events()
    )


def test_slack_personal_send_rejects_non_dm_channel_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(channel_id="CNOTDM")
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="CNOTDM"), client)
    message = OutboundMessage(
        surface="personal_chat",
        recipient_id="me",
        message_type="approval_request",
        text="개인 DM 안전가드",
        proposal_id="proposal-1",
        approval_request_id="approval-1",
    )

    with pytest.raises(SlackAdapterError, match="D... conversation id"):
        dispatch_slack_outbound(store, adapter, (message,), sent_at=NOW)

    assert client.sent == []


def test_slack_live_config_doctor_accepts_private_dm_bot_setup_without_leaking_token() -> None:
    check = diagnose_slack_live_config(
        SlackDmConfig(
            actor_id="me",
            dm_channel_id="D123",
            bot_token="xoxb-test",
            bot_user_id="U_BOT",
        )
    )

    assert check.ok is True
    assert check.can_poll is True
    assert check.can_send is True
    assert check.token_kind == "bot"
    assert check.dm_resolution == "SLACK_DM_CHANNEL_ID"
    assert set(check.required_bot_scopes) == {"chat:write", "im:history", "im:write"}
    assert "xoxb-test" not in repr(check)


def test_slack_dm_config_repr_does_not_expose_bot_token() -> None:
    assert "xoxb-test" not in repr(SlackDmConfig(bot_token="xoxb-test"))


def test_slack_live_config_doctor_reports_missing_token_and_target() -> None:
    check = diagnose_slack_live_config(SlackDmConfig())

    assert check.ok is False
    assert check.can_poll is False
    assert check.can_send is False
    assert check.token_kind == "missing"
    assert any("SLACK_BOT_TOKEN" in error for error in check.errors)
    assert any("SLACK_DM_CHANNEL_ID or SLACK_USER_ID" in error for error in check.errors)


def test_slack_live_config_doctor_rejects_non_bot_token_and_non_dm_channel() -> None:
    check = diagnose_slack_live_config(SlackDmConfig(dm_channel_id="C123", bot_token="xapp-test"))

    assert check.ok is False
    assert check.token_kind == "app-level"
    assert any("xoxb-" in error for error in check.errors)
    assert any("D" in error and "SLACK_DM_CHANNEL_ID" in error for error in check.errors)


def test_slack_live_config_doctor_accepts_user_id_resolution_path() -> None:
    check = diagnose_slack_live_config(SlackDmConfig(user_id="U123", bot_token="xoxb-test"))

    assert check.ok is True
    assert check.dm_resolution == "SLACK_USER_ID via conversations.open"
    assert any("conversations.open" in warning for warning in check.warnings)


def test_slack_live_config_guard_disables_send_for_wrong_runtime_instance() -> None:
    check = diagnose_slack_live_config(
        SlackDmConfig(
            actor_id="me",
            dm_channel_id="D123",
            bot_token="xoxb-test",
            instance_id="windows-a",
            allowed_instance_id="mac-mini-b",
        )
    )

    assert check.ok is False
    assert check.can_poll is True
    assert check.can_send is False
    assert check.instance_guard_ok is False
    assert any("expected 'mac-mini-b'" in error for error in check.errors)


def test_slack_send_guard_rejects_wrong_runtime_instance() -> None:
    client = FakeSlackWebClient(channel_id="DTEST")
    adapter = SlackDmAdapter(
        SlackDmConfig(
            actor_id="me",
            dm_channel_id="DTEST",
            instance_id="windows-a",
            allowed_instance_id="mac-mini-b",
        ),
        client,
    )

    with pytest.raises(SlackAdapterError, match="expected 'mac-mini-b'"):
        adapter.send_personal("me", "should not send")

    assert client.sent == []


def test_instance_probe_replies_only_on_target_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("TASK_MANAGEMENT_INSTANCE_ID", "mac-mini-b")
    message = IncomingMessage(
        message_id="slack/DTEST/1",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text="mac-mini-only mac-mini-b nonce-1",
        received_at=NOW,
    )

    result = TeamTaskOrchestrator(store).handle_message(message)

    assert len(result.outbound_messages) == 1
    assert "MAC_MINI_B_OK nonce-1" in result.outbound_messages[0].text
    assert [event["type"] for event in store.read_events()].count("instance_probe.accepted") == 1


def test_instance_probe_is_ignored_on_non_target_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("TASK_MANAGEMENT_INSTANCE_ID", "windows-a")
    message = IncomingMessage(
        message_id="slack/DTEST/1",
        sender_id="me",
        chat_id="DTEST",
        visibility="private",
        text="mac-mini-only mac-mini-b nonce-1",
        received_at=NOW,
    )

    result = TeamTaskOrchestrator(store).handle_message(message)

    assert result.outbound_messages == ()
    assert [event["type"] for event in store.read_events()].count("instance_probe.ignored") == 1


def test_slack_poll_rejects_non_dm_channel_before_reading(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(messages=[_slack_message("1007.000001", "hello")], channel_id="CNOTDM")
    adapter = SlackDmAdapter(SlackDmConfig(actor_id="me", dm_channel_id="CNOTDM"), client)

    with pytest.raises(SlackAdapterError, match="D... conversation"):
        run_slack_dm_once(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=adapter,
            send=False,
            now=NOW,
        )

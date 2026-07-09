from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from task_management.cli import main
from task_management.domain import OutboundMessage
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.slack_adapter import (
    MAX_OUTBOUND_SEND_ATTEMPTS,
    FakeSlackWebClient,
    SlackDmAdapter,
    SlackDmConfig,
    drain_slack_outbound_queue,
    queue_slack_outbound,
)
from task_management.slack_socket import (
    MAX_INBOUND_PROCESS_ATTEMPTS,
    FakeSlackSocketClient,
    SlackSocketConfig,
    _reconnect_delay_seconds,
    diagnose_slack_socket_config,
    enqueue_slack_socket_envelope,
    handle_slack_socket_envelope,
    process_slack_socket_inbound_queue,
    run_slack_socket_loop,
    slack_socket_envelope_to_incoming,
    slack_socket_home_user_id,
)
from task_management.store import TeamTaskStore


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def datetime_from_ts(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts.split(".")[0]))


def _message_im_envelope(
    envelope_id: str = "env-1",
    *,
    channel: str = "DTEST",
    ts: str = "1779167000.000001",
    text: str = "내가 내일 보고서 확인할게",
    user: str = "UUSER",
    subtype: str = "",
    bot_id: str = "",
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "message",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
        "event_ts": ts,
        "channel_type": "im",
    }
    if subtype:
        event["subtype"] = subtype
    if bot_id:
        event["bot_id"] = bot_id
    return {
        "type": "events_api",
        "envelope_id": envelope_id,
        "accepts_response_payload": False,
        "payload": {
            "type": "event_callback",
            "event_id": f"Ev-{envelope_id}",
            "event": event,
        },
    }


def _channel_message_envelope(
    envelope_id: str = "env-channel-1",
    *,
    channel: str = "CTEST",
    ts: str = "1779168000.000001",
    text: str = "<@UUSER> 내가 내일 보고서 확인할게",
    user: str = "UOTHER",
    event_type: str = "message",
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": event_type,
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
        "event_ts": ts,
        "channel_type": "channel",
    }
    return {
        "type": "events_api",
        "envelope_id": envelope_id,
        "accepts_response_payload": False,
        "payload": {
            "type": "event_callback",
            "event_id": f"Ev-{envelope_id}",
            "event": event,
        },
    }


def _app_home_envelope(
    envelope_id: str = "env-home",
    *,
    user: str = "UUSER",
    tab: str = "home",
) -> dict[str, object]:
    return {
        "type": "events_api",
        "envelope_id": envelope_id,
        "accepts_response_payload": False,
        "payload": {
            "type": "event_callback",
            "event_id": f"Ev-{envelope_id}",
            "event": {
                "type": "app_home_opened",
                "user": user,
                "channel": "DTEST",
                "tab": tab,
                "event_ts": "1779167000.000002",
            },
        },
    }


def _config() -> SlackSocketConfig:
    return SlackSocketConfig(
        app_token="xapp-test",
        dm_config=SlackDmConfig(actor_id="me", dm_channel_id="DTEST", bot_token="xoxb-test"),
    )


def test_socket_config_requires_app_level_token_without_leaking_value() -> None:
    missing = diagnose_slack_socket_config(
        SlackSocketConfig(app_token="", dm_config=SlackDmConfig(actor_id="me", dm_channel_id="DTEST", bot_token="xoxb-test"))
    )
    ready = diagnose_slack_socket_config(_config())

    assert missing.ok is False
    assert missing.app_token_kind == "missing"
    assert "SLACK_APP_TOKEN" in missing.errors[0]
    assert ready.ok is True
    assert ready.app_token_kind == "app-level"
    assert ready.required_app_scopes == ("connections:write",)
    assert ready.watched_channel_ids == ()
    assert ready.watch_requires_mention is True


def test_socket_handle_marks_eyes_then_check_reactions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = FakeSlackWebClient(channel_id="DTEST")
    adapter = SlackDmAdapter(
        SlackDmConfig(actor_id="me", dm_channel_id="DTEST", bot_token="xoxb-test"), client
    )
    orchestrator = TeamTaskOrchestrator(store)

    handle_slack_socket_envelope(
        store=store,
        orchestrator=orchestrator,
        adapter=adapter,
        envelope=_message_im_envelope(),
        dashboard_output=tmp_path / "dashboard.html",
        send=True,
        handled_at=datetime(2026, 5, 5, 10, 0, 0),
    )

    added = [name for _c, _ts, name in client.reactions_added]
    removed = [name for _c, _ts, name in client.reactions_removed]
    assert "eyes" in added
    assert "white_check_mark" in added
    assert "eyes" in removed
    assert ("DTEST", "1779167000.000001", "white_check_mark") in client.reactions_added


def test_socket_message_im_envelope_converts_to_incoming_message() -> None:
    message = slack_socket_envelope_to_incoming(_message_im_envelope(), config=_config().dm_config)

    assert message is not None
    assert message.message_id == "slack/DTEST/1779167000.000001"
    assert message.sender_id == "me"
    assert message.visibility == "private"
    assert message.text == "내가 내일 보고서 확인할게"


def test_socket_allowlisted_mention_channel_message_converts_to_team_incoming() -> None:
    config = SlackDmConfig(
        actor_id="me",
        dm_channel_id="DTEST",
        user_id="UUSER",
        bot_token="xoxb-test",
        watched_channel_ids=("CTEST",),
    )

    message = slack_socket_envelope_to_incoming(_channel_message_envelope(), config=config, expected_channel_id="DTEST")

    assert message is not None
    assert message.message_id == "slack/CTEST/1779168000.000001"
    assert message.sender_id == "me"
    assert message.chat_id == "CTEST"
    assert message.visibility == "team"
    assert message.text == "<@UUSER> 내가 내일 보고서 확인할게"
    assert (
        slack_socket_envelope_to_incoming(
            _channel_message_envelope(channel="COTHER"),
            config=config,
            expected_channel_id="DTEST",
        )
        is None
    )
    assert (
        slack_socket_envelope_to_incoming(
            _channel_message_envelope(text="내가 내일 보고서 확인할게"),
            config=config,
            expected_channel_id="DTEST",
        )
        is None
    )


def test_socket_allowlisted_own_channel_message_converts_without_mention() -> None:
    config = SlackDmConfig(
        actor_id="me",
        dm_channel_id="DTEST",
        user_id="UUSER",
        bot_token="xoxb-test",
        watched_channel_ids=("CTEST",),
        watch_requires_mention=True,
    )

    message = slack_socket_envelope_to_incoming(
        _channel_message_envelope(text="내가 내일 보고서 확인할게", user="UUSER"),
        config=config,
        expected_channel_id="DTEST",
    )

    assert message is not None
    assert message.message_id == "slack/CTEST/1779168000.000001"
    assert message.visibility == "team"
    assert message.text == "내가 내일 보고서 확인할게"


def test_socket_app_home_event_is_limited_to_configured_user() -> None:
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST", user_id="UUSER", bot_token="xoxb-test")

    assert slack_socket_home_user_id(_app_home_envelope(), config=config) == "UUSER"
    assert slack_socket_home_user_id(_app_home_envelope(user="UOTHER"), config=config) == ""
    assert slack_socket_home_user_id(_app_home_envelope(tab="messages"), config=config) == ""
    assert slack_socket_home_user_id(_app_home_envelope(), config=_config().dm_config) == ""


def test_socket_ignores_non_personal_or_bot_messages() -> None:
    config = _config().dm_config

    assert slack_socket_envelope_to_incoming(_message_im_envelope(channel="CTEST"), config=config) is None
    assert slack_socket_envelope_to_incoming(_message_im_envelope(channel="DOTHER"), config=config) is None
    assert slack_socket_envelope_to_incoming(_message_im_envelope(subtype="bot_message"), config=config) is None
    assert slack_socket_envelope_to_incoming(_message_im_envelope(bot_id="BTEST"), config=config) is None


def test_socket_enforces_resolved_dm_channel_when_config_started_with_user_id_only() -> None:
    config = SlackDmConfig(actor_id="me", user_id="UUSER", bot_token="xoxb-test")

    assert (
        slack_socket_envelope_to_incoming(
            _message_im_envelope(channel="DTEST"),
            config=config,
            expected_channel_id="DTEST",
        )
        is not None
    )
    assert (
        slack_socket_envelope_to_incoming(
            _message_im_envelope(channel="DOTHER"),
            config=config,
            expected_channel_id="DTEST",
        )
        is None
    )


def test_socket_loop_acks_processes_sends_and_renders_dashboard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = FakeSlackSocketClient(
        envelopes=[
            {"type": "hello", "num_connections": 1},
            _message_im_envelope(),
        ]
    )
    dashboard = tmp_path / "out" / "dashboard.html"

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(_config().dm_config, web_client),
            socket_config=_config(),
            socket_client=socket_client,
            dashboard_output=dashboard,
            send=True,
            max_events=1,
            reconnect=False,
        )
    )

    assert result.connected is True
    assert result.event_count == 1
    assert result.message_count == 1
    assert result.result_count == 1
    assert result.outbound_count == 1
    assert socket_client.acks == [{"envelope_id": "env-1"}]
    assert len(store.list_proposals()) == 1
    assert store.get_integration_state("slack.dm.me.last_ts") == "1779167000.000001"
    assert len(web_client.sent) == 1
    assert "[팀 공유 기록]" not in web_client.sent[0][2]
    assert "보고서 확인" in dashboard.read_text(encoding="utf-8")
    event_types = [event["type"] for event in store.read_events()]
    assert "slack.socket.event.received" in event_types
    assert "slack.socket.event.handled" in event_types
    assert "slack.socket.loop.stopped" in event_types


def test_socket_loop_publishes_app_home_on_home_open(tmp_path: Path) -> None:
    store = _store(tmp_path)
    message = slack_socket_envelope_to_incoming(_message_im_envelope(), config=_config().dm_config)
    assert message is not None
    TeamTaskOrchestrator(store).handle_message(message)
    config = SlackSocketConfig(
        app_token="xapp-test",
        dm_config=SlackDmConfig(actor_id="me", dm_channel_id="DTEST", user_id="UUSER", bot_token="xoxb-test"),
    )
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = FakeSlackSocketClient(envelopes=[_app_home_envelope()])

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(config.dm_config, web_client),
            socket_config=config,
            socket_client=socket_client,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            home_dashboard_url="http://127.0.0.1:8766/dashboard.html",
            send=True,
            max_events=1,
            reconnect=False,
        )
    )

    assert result.event_count == 1
    assert result.handled_events[0].home_user_id == "UUSER"
    assert result.handled_events[0].home_published is True
    assert socket_client.acks == [{"envelope_id": "env-home"}]
    assert len(web_client.home_views) == 1
    user_id, view, _view_id = web_client.home_views[0]
    assert user_id == "UUSER"
    assert view["type"] == "home"
    assert "Task Management" in json.dumps(view, ensure_ascii=False)
    assert "전체 1" in json.dumps(view, ensure_ascii=False)
    assert any(event["type"] == "slack.home.published" for event in store.read_events())


def test_socket_allowlisted_notification_requires_user_confirmation_before_task_approval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = SlackSocketConfig(
        app_token="xapp-test",
        dm_config=SlackDmConfig(
            actor_id="me",
            dm_channel_id="DTEST",
            user_id="UUSER",
            bot_token="xoxb-test",
            watched_channel_ids=("CTEST",),
        ),
    )
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = FakeSlackSocketClient(envelopes=[_channel_message_envelope()])

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(config.dm_config, web_client),
            socket_config=config,
            socket_client=socket_client,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            send=True,
            max_events=1,
            reconnect=False,
        )
    )

    proposals = store.list_proposals()
    requests = store.list_approval_requests(status="pending")

    assert result.event_count == 1
    assert result.message_count == 1
    assert result.outbound_count == 1
    assert len(proposals) == 1
    assert proposals[0].status == "awaiting_approval"
    assert proposals[0].approvals == ()
    assert proposals[0].required_approvers == ("me",)
    assert proposals[0].metadata["notification_task_candidate"] == "true"
    assert proposals[0].metadata["confirmation_required"] == "true"
    assert proposals[0].metadata["intake_policy_reason"] == "slack_allowlisted_notification"
    assert len(requests) == 1
    assert requests[0].approver_id == "me"
    assert store.get_integration_state("slack.dm.me.last_ts") is None
    assert store.get_integration_state("slack.watch.CTEST.last_ts") == "1779168000.000001"
    assert len(web_client.sent) == 1
    sent_text = web_client.sent[0][2]
    assert "Slack 알림에서 task 후보로 보입니다" in sent_text
    assert f"수락 {requests[0].request_id}" in sent_text
    assert "proposal.approved" not in [event["type"] for event in store.read_events()]


def test_socket_loop_duplicate_retry_does_not_duplicate_proposals_or_sends(tmp_path: Path) -> None:
    store = _store(tmp_path)
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = FakeSlackSocketClient(
        envelopes=[
            _message_im_envelope("env-1"),
            _message_im_envelope("env-2"),
        ]
    )

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(_config().dm_config, web_client),
            socket_config=_config(),
            socket_client=socket_client,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            send=True,
            max_events=2,
            reconnect=False,
        )
    )

    assert result.event_count == 2
    assert socket_client.acks == [{"envelope_id": "env-1"}, {"envelope_id": "env-2"}]
    assert len(store.list_proposals()) == 1
    assert len(web_client.sent) == 1
    assert result.handled_events[1].result is not None
    assert result.handled_events[1].result.ignored_duplicate is True


def test_socket_event_queue_survives_restart_before_worker_processes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _message_im_envelope()

    inserted = enqueue_slack_socket_envelope(
        store=store,
        adapter=SlackDmAdapter(_config().dm_config, FakeSlackWebClient(channel_id="DTEST")),
        envelope=envelope,
        received_at=datetime_from_ts("1779167000.000001"),
    )

    assert inserted is True
    assert len(store.list_pending_inbound_events(provider="slack_socket")) == 1

    restarted_store = _store(tmp_path)
    web_client = FakeSlackWebClient(channel_id="DTEST")
    handled = process_slack_socket_inbound_queue(
        store=restarted_store,
        orchestrator=TeamTaskOrchestrator(restarted_store),
        adapter=SlackDmAdapter(_config().dm_config, web_client),
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
        handled_at=datetime_from_ts("1779167001.000001"),
    )

    assert len(handled) == 1
    assert handled[0].message is not None
    assert len(restarted_store.list_pending_inbound_events(provider="slack_socket")) == 0
    assert len(restarted_store.list_pending_outbound_messages(provider="slack")) == 0
    assert len(restarted_store.list_proposals()) == 1
    assert len(web_client.sent) == 1
    event_types = [event["type"] for event in restarted_store.read_events()]
    assert "slack.socket.event.received" in event_types
    assert "slack.message.queued" in event_types
    assert "slack.message.sent" in event_types


def test_socket_loop_reconnects_after_transient_websocket_error(tmp_path: Path) -> None:
    class FlakySocketClient:
        def __init__(self) -> None:
            self.open_count = 0
            self.acks: list[dict[str, object]] = []

        def open_connection(self, app_token: str) -> str:
            self.open_count += 1
            return f"wss://fake.slack/socket/{self.open_count}"

        async def iter_envelopes(self, url: str):
            if self.open_count == 1:
                yield {"type": "hello", "num_connections": 1}
                raise RuntimeError("socket dropped")
            yield _message_im_envelope()

        async def ack(self, envelope_id: str, payload=None) -> None:
            item: dict[str, object] = {"envelope_id": envelope_id}
            if payload is not None:
                item["payload"] = dict(payload)
            self.acks.append(item)

    store = _store(tmp_path)
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = FlakySocketClient()

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(_config().dm_config, web_client),
            socket_config=_config(),
            socket_client=socket_client,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            send=True,
            max_events=1,
            reconnect=True,
        )
    )

    assert socket_client.open_count == 2
    assert result.connected is True
    assert result.stopped_reason == "max_events"
    assert result.event_count == 1
    assert len(web_client.sent) == 1
    assert socket_client.acks == [{"envelope_id": "env-1"}]
    event_types = [event["type"] for event in store.read_events()]
    assert "slack.socket.connection.error" in event_types
    assert "slack.socket.event.handled" in event_types


def test_slack_socket_loop_cli_processes_local_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    for _key in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_DM_CHANNEL_ID",
        "SLACK_USER_ID",
        "TASK_MANAGEMENT_INSTANCE_ID",
        "TASK_MANAGEMENT_ALLOWED_INSTANCE_ID",
    ):
        monkeypatch.delenv(_key, raising=False)
    transcript = tmp_path / "events.json"
    transcript.write_text(json.dumps([_message_im_envelope()], ensure_ascii=False), encoding="utf-8")
    dashboard = tmp_path / "dashboard.html"

    main(
        [
            "--state",
            str(tmp_path / "state"),
            "slack-socket-loop",
            "--transcript-input",
            str(transcript),
            "--dashboard-output",
            str(dashboard),
            "--send",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["event_count"] == 1
    assert payload["message_count"] == 1
    assert payload["outbound_count"] == 1
    assert payload["stopped_reason"] == "socket_closed"
    assert payload["fake_acks"] == [{"envelope_id": "env-1"}]
    assert payload["fake_sent"][0][0] == "DTEST"
    assert "보고서 확인" in dashboard.read_text(encoding="utf-8")


# --- BUG A2 (#4): failed outbound sends are retried until a terminal cap ---


class _SendOnceFailsThenSucceedsClient(FakeSlackWebClient):
    def __init__(self, *, fail_times: int, channel_id: str = "DTEST") -> None:
        super().__init__(channel_id=channel_id)
        self._fail_times = fail_times
        self.attempts = 0

    def send_message(self, channel_id: str, text: str) -> str:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            from task_management.slack_adapter import SlackAdapterError

            raise SlackAdapterError("transient Slack failure")
        return super().send_message(channel_id, text)


def _approval_outbound(message_type: str = "approval_request") -> OutboundMessage:
    return OutboundMessage(
        surface="personal_chat",
        recipient_id="me",
        message_type=message_type,
        text="확인이 필요합니다",
        approval_request_id="approval/abc123",
    )


def test_outbound_send_failure_is_retried_until_terminal_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST")
    client = _SendOnceFailsThenSucceedsClient(fail_times=1)
    adapter = SlackDmAdapter(config, client)
    message = _approval_outbound()
    now = datetime(2026, 5, 5, 10, 0, 0)

    queue_slack_outbound(store, adapter, (message,), queued_at=now)
    pending = store.list_pending_outbound_messages(provider="slack")
    assert len(pending) == 1
    dedupe_key = pending[0]["dedupe_key"]

    # First drain fails the send but leaves the row retryable (status='pending').
    sent_first = drain_slack_outbound_queue(store, adapter, sent_at=now, raise_on_error=False)
    assert sent_first == 0
    still_pending = store.list_pending_outbound_messages(provider="slack")
    assert len(still_pending) == 1
    assert still_pending[0]["attempts"] == 1
    failed_events = [e for e in store.read_events() if e["type"] == "slack.message.failed"]
    assert failed_events[-1]["payload"]["attempts"] == 1
    assert failed_events[-1]["payload"]["retryable"] is True

    # Second drain actually resends because the row stayed pending.
    sent_second = drain_slack_outbound_queue(
        store, adapter, sent_at=now.replace(minute=5), raise_on_error=False
    )
    assert sent_second == 1
    assert store.list_pending_outbound_messages(provider="slack") == ()
    assert store.has_outbound_delivery(dedupe_key) is True
    assert len(client.sent) == 1


def test_outbound_send_failure_becomes_terminal_after_max_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST")
    # Always fails: never succeeds within the cap.
    client = _SendOnceFailsThenSucceedsClient(fail_times=MAX_OUTBOUND_SEND_ATTEMPTS + 1)
    adapter = SlackDmAdapter(config, client)
    message = _approval_outbound()
    now = datetime(2026, 5, 5, 10, 0, 0)

    queue_slack_outbound(store, adapter, (message,), queued_at=now)

    for attempt in range(1, MAX_OUTBOUND_SEND_ATTEMPTS + 1):
        drain_slack_outbound_queue(
            store, adapter, sent_at=now.replace(minute=attempt), raise_on_error=False
        )
        rows = store.list_pending_outbound_messages(provider="slack")
        if attempt < MAX_OUTBOUND_SEND_ATTEMPTS:
            assert len(rows) == 1, f"row should stay pending on attempt {attempt}"
        else:
            assert rows == (), "row should be terminal after the cap"

    failed_events = [e for e in store.read_events() if e["type"] == "slack.message.failed"]
    assert failed_events[-1]["payload"]["attempts"] == MAX_OUTBOUND_SEND_ATTEMPTS
    assert failed_events[-1]["payload"]["retryable"] is False
    # Terminal row is no longer drained on the next pass.
    assert drain_slack_outbound_queue(store, adapter, sent_at=now.replace(hour=11)) == 0


# --- BUG A1 (#5): per-message salt keeps distinct replies from colliding ---


def test_outbound_distinct_source_messages_both_deliver_same_stable_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST")
    adapter = SlackDmAdapter(config, FakeSlackWebClient(channel_id="DTEST"))
    now = datetime(2026, 5, 5, 10, 0, 0)

    # Two distinct inbound messages produce the same message_type +
    # approval_request_id (e.g. repeated agent_patch_rejected) -> historically
    # collided on one dedupe key and the second was suppressed forever.
    message = _approval_outbound(message_type="agent_patch_rejected")

    queued_a = queue_slack_outbound(
        store, adapter, (message,), queued_at=now, source_message_id="slack/DTEST/1.000001"
    )
    queued_b = queue_slack_outbound(
        store, adapter, (message,), queued_at=now, source_message_id="slack/DTEST/2.000002"
    )
    # Re-polling the SAME inbound yields the same source id -> still deduped.
    queued_repoll = queue_slack_outbound(
        store, adapter, (message,), queued_at=now, source_message_id="slack/DTEST/1.000001"
    )

    assert queued_a == 1
    assert queued_b == 1
    assert queued_repoll == 0
    assert len(store.list_pending_outbound_messages(provider="slack")) == 2


def test_outbound_explicit_dedupe_key_ignores_source_message_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST")
    adapter = SlackDmAdapter(config, FakeSlackWebClient(channel_id="DTEST"))
    now = datetime(2026, 5, 5, 10, 0, 0)

    message = OutboundMessage(
        surface="personal_chat",
        recipient_id="me",
        message_type="morning_briefing",
        text="아침 브리핑",
        card={"dedupe_key": "briefing/2026-05-05"},
    )

    queued_a = queue_slack_outbound(
        store, adapter, (message,), queued_at=now, source_message_id="slack/DTEST/1.000001"
    )
    # Different source id, but the explicit card dedupe_key must still collapse.
    queued_b = queue_slack_outbound(
        store, adapter, (message,), queued_at=now, source_message_id="slack/DTEST/2.000002"
    )

    assert queued_a == 1
    assert queued_b == 0
    assert len(store.list_pending_outbound_messages(provider="slack")) == 1


# --- BUG A4 (#14): a store failure during enqueue must not kill the loop ---


def test_socket_loop_survives_enqueue_store_failure_and_keeps_running(tmp_path: Path) -> None:
    real_store = _store(tmp_path)

    class _EnqueueFailsOnceStore:
        def __init__(self, inner: TeamTaskStore) -> None:
            self._inner = inner
            self.enqueue_calls = 0

        def enqueue_inbound_event(self, **kwargs):
            self.enqueue_calls += 1
            if self.enqueue_calls == 1:
                raise RuntimeError("database is locked")
            return self._inner.enqueue_inbound_event(**kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    store = _EnqueueFailsOnceStore(real_store)
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = FakeSlackSocketClient(
        envelopes=[
            _message_im_envelope("env-1", ts="1779167000.000001"),
            _message_im_envelope("env-2", ts="1779167000.000002"),
        ]
    )

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(real_store),
            adapter=SlackDmAdapter(_config().dm_config, web_client),
            socket_config=_config(),
            socket_client=socket_client,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            send=True,
            max_events=2,
            reconnect=False,
        )
    )

    event_types = [event["type"] for event in real_store.read_events()]
    assert "slack.socket.enqueue.failed" in event_types
    # The failed envelope was NOT acked (so Slack redelivers it)...
    assert {"envelope_id": "env-1"} not in socket_client.acks
    # ...and the loop kept running and acked the next envelope.
    assert {"envelope_id": "env-2"} in socket_client.acks
    # The failed envelope is skipped before the per-event counter, so only the
    # surviving envelope counts.
    assert result.event_count == 1


# --- BUG A3 (#6): failed inbound events retry and re-process cleanly ---


class _HandleFailsOnceOrchestrator(TeamTaskOrchestrator):
    def __init__(self, store: TeamTaskStore, *, fail_times: int) -> None:
        super().__init__(store)
        self._fail_times = fail_times
        self.calls = 0

    def handle_message(self, message):
        self.calls += 1
        if self.calls <= self._fail_times:
            # Mirror the real orchestrator: record the message BEFORE the
            # fallible work, so a mid-processing crash leaves has_message=True
            # unless the retry path deletes the half-recorded row.
            self.store.record_message(message)
            raise RuntimeError("mid-processing crash")
        # Success path delegates to the real orchestrator, which performs its
        # own has_message guard + record_message; it only reaches creation
        # because the failed attempt's record was deleted on retry.
        return super().handle_message(message)


def test_socket_inbound_failure_retries_and_reprocesses_after_message_delete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _message_im_envelope()

    enqueue_slack_socket_envelope(
        store=store,
        adapter=SlackDmAdapter(_config().dm_config, FakeSlackWebClient(channel_id="DTEST")),
        envelope=envelope,
        received_at=datetime_from_ts("1779167000.000001"),
    )
    [event_row] = store.list_pending_inbound_events(provider="slack_socket")
    message_id = event_row["message_id"]
    assert message_id

    orchestrator = _HandleFailsOnceOrchestrator(store, fail_times=1)
    adapter = SlackDmAdapter(_config().dm_config, FakeSlackWebClient(channel_id="DTEST"))

    # Attempt 1: handle_message raises -> event returns to 'pending' (retryable)
    # and the half-recorded message row is deleted so the retry is not a no-op.
    handled_first = process_slack_socket_inbound_queue(
        store=store,
        orchestrator=orchestrator,
        adapter=adapter,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
        handled_at=datetime_from_ts("1779167001.000001"),
    )
    assert handled_first == ()
    still_pending = store.list_pending_inbound_events(provider="slack_socket")
    assert len(still_pending) == 1
    assert still_pending[0]["attempts"] == 1
    assert store.has_message(message_id) is False  # deleted so retry reprocesses
    failed_events = [e for e in store.read_events() if e["type"] == "slack.socket.event.failed"]
    assert failed_events[-1]["payload"]["attempts"] == 1
    assert failed_events[-1]["payload"]["retryable"] is True

    # Attempt 2: succeeds and produces a real result (proves the retry reprocessed).
    handled_second = process_slack_socket_inbound_queue(
        store=store,
        orchestrator=orchestrator,
        adapter=adapter,
        dashboard_output=tmp_path / "out" / "dashboard.html",
        send=True,
        handled_at=datetime_from_ts("1779167002.000001"),
    )
    assert len(handled_second) == 1
    assert handled_second[0].message is not None
    assert store.list_pending_inbound_events(provider="slack_socket") == ()
    assert len(store.list_proposals()) == 1


def test_socket_inbound_failure_becomes_terminal_after_max_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _message_im_envelope()

    enqueue_slack_socket_envelope(
        store=store,
        adapter=SlackDmAdapter(_config().dm_config, FakeSlackWebClient(channel_id="DTEST")),
        envelope=envelope,
        received_at=datetime_from_ts("1779167000.000001"),
    )

    orchestrator = _HandleFailsOnceOrchestrator(store, fail_times=MAX_INBOUND_PROCESS_ATTEMPTS + 1)
    adapter = SlackDmAdapter(_config().dm_config, FakeSlackWebClient(channel_id="DTEST"))

    for attempt in range(1, MAX_INBOUND_PROCESS_ATTEMPTS + 1):
        process_slack_socket_inbound_queue(
            store=store,
            orchestrator=orchestrator,
            adapter=adapter,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            send=True,
            handled_at=datetime_from_ts(f"177916700{attempt}.000001"),
        )
        pending = store.list_pending_inbound_events(provider="slack_socket")
        if attempt < MAX_INBOUND_PROCESS_ATTEMPTS:
            assert len(pending) == 1, f"event should stay pending on attempt {attempt}"
        else:
            assert pending == (), "event should be terminal after the cap"

    failed_events = [e for e in store.read_events() if e["type"] == "slack.socket.event.failed"]
    assert failed_events[-1]["payload"]["attempts"] == MAX_INBOUND_PROCESS_ATTEMPTS
    assert failed_events[-1]["payload"]["retryable"] is False


# --- BUG #13: reconnect uses bounded exponential backoff and a protected open ---


def test_reconnect_delay_seconds_grows_with_attempt_and_is_capped() -> None:
    # Pin jitter to its midpoint so the assertion is deterministic.
    import task_management.slack_socket as socket_module

    original_uniform = socket_module.random.uniform
    socket_module.random.uniform = lambda a, b: (a + b) / 2.0
    try:
        delays = [_reconnect_delay_seconds(attempt, base=1.0, cap=30.0) for attempt in range(8)]
    finally:
        socket_module.random.uniform = original_uniform

    # Strictly grows until it saturates, never exceeds the cap, and is positive.
    assert delays[0] < delays[1] < delays[2] < delays[3]
    assert all(0.0 < delay <= 30.0 for delay in delays)
    # High attempts saturate at the cap.
    assert delays[-1] == 30.0
    assert _reconnect_delay_seconds(100, base=1.0, cap=30.0) == 30.0
    # A different cap is honored.
    assert _reconnect_delay_seconds(100, base=1.0, cap=5.0) == 5.0


def test_socket_loop_recovers_from_open_connection_failure_with_bounded_backoff(
    tmp_path: Path,
) -> None:
    class OpenFailsOnceClient:
        def __init__(self) -> None:
            self.open_count = 0
            self.acks: list[dict[str, object]] = []

        def open_connection(self, app_token: str) -> str:
            self.open_count += 1
            if self.open_count == 1:
                raise RuntimeError("apps.connections.open rate limited")
            return f"wss://fake.slack/socket/{self.open_count}"

        async def iter_envelopes(self, url: str):
            yield {"type": "hello", "num_connections": 1}
            yield _message_im_envelope()

        async def ack(self, envelope_id: str, payload=None) -> None:
            item: dict[str, object] = {"envelope_id": envelope_id}
            if payload is not None:
                item["payload"] = dict(payload)
            self.acks.append(item)

    store = _store(tmp_path)
    web_client = FakeSlackWebClient(channel_id="DTEST")
    socket_client = OpenFailsOnceClient()
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        # Record the computed backoff instead of really waiting (fast + bounded).
        sleeps.append(delay)

    result = asyncio.run(
        run_slack_socket_loop(
            store=store,
            orchestrator=TeamTaskOrchestrator(store),
            adapter=SlackDmAdapter(_config().dm_config, web_client),
            socket_config=_config(),
            socket_client=socket_client,
            dashboard_output=tmp_path / "out" / "dashboard.html",
            send=True,
            max_events=1,
            reconnect=True,
            sleep=fake_sleep,
        )
    )

    # The first open raised, the loop retried after a single bounded backoff, and
    # then processed the subsequent envelope without crashing.
    assert socket_client.open_count == 2
    assert result.connected is True
    assert result.stopped_reason == "max_events"
    assert result.event_count == 1
    assert len(web_client.sent) == 1
    assert socket_client.acks == [{"envelope_id": "env-1"}]
    # Exactly one backoff sleep happened and it was bounded by the cap.
    assert len(sleeps) == 1
    assert 0.0 < sleeps[0] <= 30.0
    event_types = [event["type"] for event in store.read_events()]
    assert "slack.socket.connection.error" in event_types
    assert "slack.socket.event.handled" in event_types
    connection_errors = [
        event for event in store.read_events() if event["type"] == "slack.socket.connection.error"
    ]
    assert connection_errors[0]["payload"]["error_type"] == "RuntimeError"
    assert "rate limited" in connection_errors[0]["payload"]["error"]

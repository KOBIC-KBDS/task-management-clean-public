from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from task_management.cli import main
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.slack_adapter import FakeSlackWebClient, SlackDmAdapter, SlackDmConfig
from task_management.slack_socket import (
    FakeSlackSocketClient,
    SlackSocketConfig,
    diagnose_slack_socket_config,
    enqueue_slack_socket_envelope,
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


def test_slack_socket_loop_cli_processes_local_transcript(tmp_path: Path, capsys) -> None:
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

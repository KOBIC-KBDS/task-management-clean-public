from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Protocol
from urllib import request as urlrequest

from .domain import IncomingMessage, OrchestrationResult, OutboundMessage
from .frontend import build_web_task_page_model, render_web_task_page_html
from .orchestrator import TeamTaskOrchestrator
from .slack_adapter import (
    SlackAdapterError,
    SlackDmAdapter,
    SlackDmConfig,
    drain_slack_outbound_queue,
    diagnose_slack_live_config,
    is_slack_personal_dm_channel_id,
    queue_slack_outbound,
    slack_message_to_incoming,
)
from .slack_home import publish_slack_home_tab
from .store import TeamTaskStore


SLACK_SOCKET_APP_SCOPES = ("connections:write",)


class SlackSocketError(SlackAdapterError):
    """Raised for Slack Socket Mode configuration/protocol failures."""


@dataclass(frozen=True)
class SlackSocketConfig:
    app_token: str = field(default="", repr=False)
    dm_config: SlackDmConfig = field(default_factory=SlackDmConfig.from_env)

    @classmethod
    def from_env(cls) -> "SlackSocketConfig":
        return cls(
            app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            dm_config=SlackDmConfig.from_env(),
        )


@dataclass(frozen=True)
class SlackSocketConfigCheck:
    ok: bool
    app_token_set: bool
    app_token_kind: str
    required_app_scopes: tuple[str, ...]
    dm_ok: bool
    watched_channel_ids: tuple[str, ...] = ()
    watch_requires_mention: bool = True
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    setup_steps: tuple[str, ...] = ()


def diagnose_slack_socket_config(config: SlackSocketConfig) -> SlackSocketConfigCheck:
    errors: list[str] = []
    warnings: list[str] = []
    app_token_kind = _slack_token_kind(config.app_token)
    if not config.app_token:
        errors.append("SLACK_APP_TOKEN is required for Socket Mode.")
    elif app_token_kind != "app-level":
        errors.append(
            f"SLACK_APP_TOKEN must be an app-level token beginning with xapp-; got {app_token_kind} token."
        )

    dm_check = diagnose_slack_live_config(config.dm_config)
    errors.extend(f"DM config: {item}" for item in dm_check.errors)
    warnings.extend(f"DM config: {item}" for item in dm_check.warnings)
    setup_steps = [
        "Enable Socket Mode in the Slack app settings.",
        "Generate an app-level token with connections:write and set SLACK_APP_TOKEN.",
        "Enable Event Subscriptions and subscribe the bot to message.im and app_home_opened.",
        "Optional allowlisted notification triage: subscribe to app_mention or message.channels/message.groups/message.im as needed, invite the bot to watched channels, and set TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS.",
        "Keep the personal DM bot scopes available: chat:write, im:history, im:write, reactions:write.",
        "Enable App Home > Home Tab if the Slack Home tab should show task summaries.",
        "Run slack-socket-loop only for the configured personal D... DM channel.",
    ]
    return SlackSocketConfigCheck(
        ok=not errors,
        app_token_set=bool(config.app_token),
        app_token_kind=app_token_kind,
        required_app_scopes=SLACK_SOCKET_APP_SCOPES,
        dm_ok=dm_check.ok,
        watched_channel_ids=config.dm_config.watched_channel_ids,
        watch_requires_mention=config.dm_config.watch_requires_mention,
        errors=tuple(errors),
        warnings=tuple(warnings),
        setup_steps=tuple(setup_steps),
    )


class SlackSocketClient(Protocol):
    def open_connection(self, app_token: str) -> str:
        """Return a temporary Slack Socket Mode WebSocket URL."""

    def iter_envelopes(self, url: str) -> AsyncIterator[Mapping[str, Any]]:
        """Yield decoded Socket Mode envelopes from the connected WebSocket."""

    async def ack(self, envelope_id: str, payload: Mapping[str, Any] | None = None) -> None:
        """Acknowledge a Socket Mode envelope."""


class SlackSocketWebClient:
    """Tiny Socket Mode client with a stdlib Web API call and lazy WebSocket import."""

    def __init__(self) -> None:
        self._websocket: Any = None

    def open_connection(self, app_token: str) -> str:
        if not app_token:
            raise SlackSocketError("SLACK_APP_TOKEN is required for Slack Socket Mode")
        req = urlrequest.Request(
            "https://slack.com/api/apps.connections.open",
            data=b"",
            headers={
                "Authorization": f"Bearer {app_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=20) as response:  # nosec - explicit live CLI path only
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise SlackSocketError(str(payload.get("error") or payload))
        url = str(payload.get("url") or "")
        if not url:
            raise SlackSocketError("Slack apps.connections.open did not return a WebSocket URL")
        return url

    async def iter_envelopes(self, url: str) -> AsyncIterator[Mapping[str, Any]]:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - depends on runtime environment
            raise SlackSocketError(
                "The optional 'websockets' package is required for slack-socket-loop runtime."
            ) from exc
        async with websockets.connect(url) as websocket:  # type: ignore[attr-defined]  # pragma: no cover - live only
            self._websocket = websocket
            try:
                async for raw in websocket:
                    yield json.loads(str(raw))
            finally:
                self._websocket = None

    async def ack(self, envelope_id: str, payload: Mapping[str, Any] | None = None) -> None:
        if self._websocket is None:
            raise SlackSocketError("Cannot ack Socket Mode envelope before WebSocket is connected")
        response: dict[str, Any] = {"envelope_id": envelope_id}
        if payload is not None:
            response["payload"] = dict(payload)
        await self._websocket.send(json.dumps(response, ensure_ascii=False))


@dataclass
class FakeSlackSocketClient:
    envelopes: list[dict[str, Any]]
    url: str = "wss://fake.slack/socket"
    acks: list[dict[str, Any]] = field(default_factory=list)
    opened_with_token_kind: str = ""

    def open_connection(self, app_token: str) -> str:
        self.opened_with_token_kind = _slack_token_kind(app_token)
        return self.url

    async def iter_envelopes(self, url: str) -> AsyncIterator[Mapping[str, Any]]:
        for envelope in list(self.envelopes):
            yield envelope

    async def ack(self, envelope_id: str, payload: Mapping[str, Any] | None = None) -> None:
        item: dict[str, Any] = {"envelope_id": envelope_id}
        if payload is not None:
            item["payload"] = dict(payload)
        self.acks.append(item)


@dataclass(frozen=True)
class SlackSocketEventResult:
    envelope_id: str
    envelope_type: str
    message: IncomingMessage | None
    result: OrchestrationResult | None
    outbound_messages: tuple[OutboundMessage, ...] = ()
    ignored_reason: str = ""
    home_user_id: str = ""
    home_published: bool = False


@dataclass(frozen=True)
class SlackSocketLoopResult:
    connected: bool
    event_count: int
    message_count: int
    result_count: int
    outbound_count: int
    dashboard_output: Path
    stopped_reason: str
    handled_events: tuple[SlackSocketEventResult, ...]


async def run_slack_socket_loop(
    *,
    store: TeamTaskStore,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    socket_config: SlackSocketConfig,
    socket_client: SlackSocketClient | None = None,
    dashboard_output: Path,
    send: bool = False,
    max_events: int = 0,
    max_seconds: float = 0,
    reconnect: bool = True,
    home_dashboard_url: str = "",
) -> SlackSocketLoopResult:
    """Run a personal-DM-only Slack Socket Mode loop.

    This path reuses the same state, outbound dispatch, and dashboard rendering
    as the polling fast-cycle.  It only accepts `message.im` events from the
    configured personal DM channel.
    """

    check = diagnose_slack_socket_config(socket_config)
    if not check.ok:
        raise SlackSocketError("; ".join(check.errors))

    client = socket_client or SlackSocketWebClient()
    started = time.monotonic()
    connected = False
    event_count = 0
    message_count = 0
    result_count = 0
    outbound_count = 0
    handled: list[SlackSocketEventResult] = []
    stopped_reason = "max_events"

    while True:
        url = client.open_connection(socket_config.app_token)
        store.append_event(
            "slack.socket.connection.opened",
            {"url_received": bool(url), "client": type(client).__name__},
            occurred_at=datetime.now(),
        )
        envelope_iter = client.iter_envelopes(url).__aiter__()
        socket_closed_reason = ""
        while True:
            timeout = _remaining_seconds(started, max_seconds)
            if timeout is not None and timeout <= 0:
                stopped_reason = "max_seconds"
                break
            try:
                envelope = await asyncio.wait_for(envelope_iter.__anext__(), timeout=timeout)
            except asyncio.TimeoutError:
                stopped_reason = "max_seconds"
                break
            except StopAsyncIteration:
                socket_closed_reason = "socket_closed"
                break
            except Exception as exc:
                socket_closed_reason = "socket_error"
                store.append_event(
                    "slack.socket.connection.error",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "reconnect": reconnect,
                    },
                    occurred_at=datetime.now(),
                )
                break
            envelope_type = str(envelope.get("type") or "")
            if envelope_type == "hello":
                connected = True
                store.append_event(
                    "slack.socket.connected",
                    {"type": envelope_type, "num_connections": envelope.get("num_connections", "")},
                    occurred_at=datetime.now(),
                )
                continue
            if envelope_type == "disconnect":
                store.append_event(
                    "slack.socket.disconnected",
                    {"reason": envelope.get("reason", ""), "debug_info": envelope.get("debug_info", {})},
                    occurred_at=datetime.now(),
                )
                stopped_reason = "disconnect"
                break

            envelope_id = str(envelope.get("envelope_id") or "")
            enqueue_slack_socket_envelope(
                store=store,
                adapter=adapter,
                envelope=envelope,
                received_at=datetime.now(),
            )
            ack_failed = False
            if envelope_id:
                try:
                    await client.ack(envelope_id)
                except Exception as exc:
                    # The Socket Mode WebSocket dropped mid-ack. Outbound replies go
                    # through the Slack Web API (not this socket), so keep processing
                    # the queue to deliver the reply, then reconnect instead of
                    # crashing. Slack redelivers the unacked envelope on reconnect and
                    # inbound dedupe prevents double-processing.
                    ack_failed = True
                    store.append_event(
                        "slack.socket.ack.failed",
                        {
                            "envelope_id": envelope_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                            "reconnect": reconnect,
                        },
                        occurred_at=datetime.now(),
                    )
            if send and not ack_failed:
                # Instant read receipt, applied in the receive loop BEFORE the
                # blocking operating-agent processing so the 👀 never waits on opus.
                try:
                    receipt = slack_socket_envelope_to_incoming(
                        envelope,
                        config=adapter.config,
                        expected_channel_id=adapter.channel_id,
                    )
                    if receipt is not None:
                        adapter.add_reaction(
                            receipt.chat_id, receipt.message_id.rsplit("/", 1)[-1], "eyes"
                        )
                except Exception:
                    pass
            event_count += 1
            # Run the blocking operating-agent + dispatch off the event loop so the
            # Socket Mode WebSocket keeps answering Slack pings during a long opus
            # call. Otherwise the synchronous subprocess.run froze the event loop,
            # Slack dropped the connection, and the next ack crashed the loop.
            processed = await asyncio.to_thread(
                process_slack_socket_inbound_queue,
                store=store,
                orchestrator=orchestrator,
                adapter=adapter,
                dashboard_output=dashboard_output,
                send=send,
                handled_at=datetime.now(),
                home_dashboard_url=home_dashboard_url,
            )
            handled.extend(processed)
            for event_result in processed:
                if event_result.message is not None:
                    message_count += 1
                if event_result.result is not None:
                    result_count += 1
                    outbound_count += len(event_result.outbound_messages)

            if ack_failed:
                socket_closed_reason = "ack_failed"
                break
            if max_events and event_count >= max_events:
                stopped_reason = "max_events"
                break
            if max_seconds and (time.monotonic() - started) >= max_seconds:
                stopped_reason = "max_seconds"
                break
        if socket_closed_reason:
            stopped_reason = socket_closed_reason

        if max_events and event_count >= max_events:
            break
        if max_seconds and (time.monotonic() - started) >= max_seconds:
            break
        if not reconnect:
            break
        await asyncio.sleep(1.0)

    store.append_event(
        "slack.socket.loop.stopped",
        {
            "event_count": event_count,
            "message_count": message_count,
            "result_count": result_count,
            "outbound_count": outbound_count,
            "dashboard_output": str(dashboard_output),
            "send": send,
            "stopped_reason": stopped_reason,
        },
        occurred_at=datetime.now(),
    )
    return SlackSocketLoopResult(
        connected=connected,
        event_count=event_count,
        message_count=message_count,
        result_count=result_count,
        outbound_count=outbound_count,
        dashboard_output=dashboard_output,
        stopped_reason=stopped_reason,
        handled_events=tuple(handled),
    )


def enqueue_slack_socket_envelope(
    *,
    store: TeamTaskStore,
    adapter: SlackDmAdapter,
    envelope: Mapping[str, Any],
    received_at: datetime,
) -> bool:
    envelope_id = str(envelope.get("envelope_id") or "")
    event_id = envelope_id or _stable_envelope_id(envelope)
    envelope_type = str(envelope.get("type") or "")
    message = slack_socket_envelope_to_incoming(
        envelope,
        config=adapter.config,
        expected_channel_id=adapter.channel_id,
    )
    inserted = store.enqueue_inbound_event(
        event_id=event_id,
        provider="slack_socket",
        event_type=envelope_type,
        message_id=message.message_id if message else "",
        payload=dict(envelope),
        received_at=received_at,
    )
    store.append_event(
        "slack.socket.event.received",
        {
            "envelope_id": envelope_id,
            "event_id": event_id,
            "type": envelope_type,
            "message_id": message.message_id if message else "",
            "queued": inserted,
        },
        occurred_at=received_at,
    )
    return inserted


def process_slack_socket_inbound_queue(
    *,
    store: TeamTaskStore,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    dashboard_output: Path,
    send: bool,
    handled_at: datetime,
    limit: int = 50,
    home_dashboard_url: str = "",
) -> tuple[SlackSocketEventResult, ...]:
    results: list[SlackSocketEventResult] = []
    for item in store.list_pending_inbound_events(provider="slack_socket", limit=limit):
        event_id = str(item["event_id"])
        envelope = item["payload"]
        try:
            event_result = handle_slack_socket_envelope(
                store=store,
                orchestrator=orchestrator,
                adapter=adapter,
                envelope=envelope,
                dashboard_output=dashboard_output,
                send=send,
                handled_at=handled_at,
                home_dashboard_url=home_dashboard_url,
            )
        except Exception as exc:
            store.mark_inbound_event(
                event_id,
                status="failed",
                updated_at=handled_at,
                last_error=str(exc),
                increment_attempts=True,
            )
            store.append_event(
                "slack.socket.event.failed",
                {
                    "event_id": event_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
                occurred_at=handled_at,
            )
            continue
        status = "processed" if event_result.message is not None or event_result.home_user_id else "ignored"
        store.mark_inbound_event(
            event_id,
            status=status,
            updated_at=handled_at,
            increment_attempts=True,
        )
        results.append(event_result)
    return tuple(results)


def handle_slack_socket_envelope(
    *,
    store: TeamTaskStore,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    envelope: Mapping[str, Any],
    dashboard_output: Path,
    send: bool,
    handled_at: datetime,
    home_dashboard_url: str = "",
) -> SlackSocketEventResult:
    envelope_id = str(envelope.get("envelope_id") or "")
    envelope_type = str(envelope.get("type") or "")
    expected_channel_id = adapter.channel_id
    home_user_id = slack_socket_home_user_id(envelope, config=adapter.config)
    if home_user_id:
        home_published = False
        if send:
            home_published = publish_slack_home_tab(
                store,
                adapter,
                user_id=home_user_id,
                now=handled_at,
                dashboard_url=home_dashboard_url,
            )
        store.append_event(
            "slack.home.opened",
            {
                "envelope_id": envelope_id,
                "user_id": home_user_id,
                "published": home_published,
                "send": send,
            },
            occurred_at=handled_at,
        )
        return SlackSocketEventResult(
            envelope_id=envelope_id,
            envelope_type=envelope_type,
            message=None,
            result=None,
            home_user_id=home_user_id,
            home_published=home_published,
        )
    message = slack_socket_envelope_to_incoming(
        envelope,
        config=adapter.config,
        expected_channel_id=expected_channel_id,
    )
    if message is None:
        reason = _ignored_reason(envelope, adapter.config, expected_channel_id=expected_channel_id)
        store.append_event(
            "slack.socket.event.ignored",
            {"envelope_id": envelope_id, "type": envelope_type, "reason": reason},
            occurred_at=handled_at,
        )
        return SlackSocketEventResult(
            envelope_id=envelope_id,
            envelope_type=envelope_type,
            message=None,
            result=None,
            ignored_reason=reason,
        )

    store.append_event("slack.message.polled", {"message_id": message.message_id}, occurred_at=message.received_at)
    ts = message.message_id.rsplit("/", 1)[-1]
    if send:
        try:  # best-effort read receipt; never block processing on a reaction failure
            adapter.add_reaction(message.chat_id, ts, "eyes")
        except Exception:
            pass
    result = orchestrator.handle_message(message)
    outbound = result.outbound_messages
    if message.chat_id == expected_channel_id:
        latest_ts = store.get_integration_state(_last_ts_key(adapter.config.actor_id)) or adapter.oldest
        if not latest_ts or float(ts) > float(latest_ts):
            store.set_integration_state(_last_ts_key(adapter.config.actor_id), ts, updated_at=handled_at)
    else:
        store.set_integration_state(_watch_last_ts_key(message.chat_id), ts, updated_at=handled_at)
    if send and outbound:
        queue_slack_outbound(store, adapter, tuple(outbound), queued_at=handled_at)
        drain_slack_outbound_queue(store, adapter, sent_at=handled_at)
    if send:
        try:  # mark done: eyes -> white_check_mark, best-effort
            adapter.remove_reaction(message.chat_id, ts, "eyes")
            adapter.add_reaction(message.chat_id, ts, "white_check_mark")
        except Exception:
            pass

    dashboard_output.parent.mkdir(parents=True, exist_ok=True)
    dashboard_model = build_web_task_page_model(store, today=handled_at.date())
    dashboard_output.write_text(render_web_task_page_html(dashboard_model), encoding="utf-8")
    home_published = False
    if send and home_dashboard_url:
        home_published = publish_slack_home_tab(
            store,
            adapter,
            now=handled_at,
            dashboard_url=home_dashboard_url,
        )
    store.append_event(
        "slack.socket.event.handled",
        {
            "envelope_id": envelope_id,
            "message_id": message.message_id,
            "ignored_duplicate": result.ignored_duplicate,
            "proposal_count": len(result.proposals),
            "outbound_count": len(outbound),
            "dashboard_output": str(dashboard_output),
            "home_published": home_published,
        },
        occurred_at=handled_at,
    )
    return SlackSocketEventResult(
        envelope_id=envelope_id,
        envelope_type=envelope_type,
        message=message,
        result=result,
        outbound_messages=tuple(outbound),
        home_published=home_published,
    )


def _stable_envelope_id(envelope: Mapping[str, Any]) -> str:
    payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    return f"slack-socket/{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def slack_socket_envelope_to_incoming(
    envelope: Mapping[str, Any],
    *,
    config: SlackDmConfig,
    expected_channel_id: str = "",
) -> IncomingMessage | None:
    if envelope.get("type") != "events_api":
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "event_callback":
        return None
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return None
    event_type = str(event.get("type") or "")
    if event_type not in {"message", "app_mention"}:
        return None
    channel_id = str(event.get("channel") or "")
    if event.get("subtype") or event.get("bot_id"):
        return None
    if config.bot_user_id and str(event.get("user") or "") == config.bot_user_id:
        return None
    required_channel_id = expected_channel_id or config.dm_channel_id
    if (
        event_type == "message"
        and event.get("channel_type") == "im"
        and is_slack_personal_dm_channel_id(channel_id)
        and (not required_channel_id or channel_id == required_channel_id)
    ):
        return slack_message_to_incoming(event, config=config, channel_id=channel_id)
    if _is_allowlisted_notification_event(
        event,
        config=config,
        expected_channel_id=required_channel_id,
    ):
        return slack_message_to_incoming(event, config=config, channel_id=channel_id, visibility="team")
    return None


def slack_socket_home_user_id(envelope: Mapping[str, Any], *, config: SlackDmConfig) -> str:
    if envelope.get("type") != "events_api":
        return ""
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "event_callback":
        return ""
    event = payload.get("event")
    if not isinstance(event, Mapping) or event.get("type") != "app_home_opened":
        return ""
    tab = str(event.get("tab") or "home")
    if tab != "home":
        return ""
    user_id = str(event.get("user") or "")
    if not user_id:
        return ""
    if not config.user_id:
        return ""
    if user_id != config.user_id:
        return ""
    return user_id


def _ignored_reason(envelope: Mapping[str, Any], config: SlackDmConfig, *, expected_channel_id: str = "") -> str:
    if envelope.get("type") != "events_api":
        return "not_events_api"
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "event_callback":
        return "not_event_callback"
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return "missing_event"
    if event.get("type") == "app_home_opened":
        user_id = str(event.get("user") or "")
        tab = str(event.get("tab") or "home")
        if tab != "home":
            return "not_home_tab"
        if not config.user_id:
            return "missing_home_user_id"
        if user_id != config.user_id:
            return "wrong_home_user"
        return "home_unhandled"
    event_type = str(event.get("type") or "")
    if event_type not in {"message", "app_mention"}:
        return "not_message"
    channel_id = str(event.get("channel") or "")
    required_channel_id = expected_channel_id or config.dm_channel_id
    if event.get("subtype") or event.get("bot_id"):
        return "bot_or_subtype"
    if config.bot_user_id and str(event.get("user") or "") == config.bot_user_id:
        return "self_message"
    if not str(event.get("text") or "").strip():
        return "empty_text"
    if (
        event_type == "message"
        and event.get("channel_type") == "im"
        and is_slack_personal_dm_channel_id(channel_id)
        and (not required_channel_id or channel_id == required_channel_id)
    ):
        return "unknown"
    if channel_id not in set(config.watched_channel_ids):
        if event.get("channel_type") == "im" and is_slack_personal_dm_channel_id(channel_id):
            return "wrong_dm_channel"
        return "unwatched_channel"
    if (
        config.watch_requires_mention
        and not _mentions_configured_user(str(event.get("text") or ""), config=config)
        and str(event.get("user") or "") != config.user_id
    ):
        return "watch_requires_mention"
    return "unknown"


def _last_ts_key(actor_id: str) -> str:
    return f"slack.dm.{actor_id}.last_ts"


def _watch_last_ts_key(channel_id: str) -> str:
    return f"slack.watch.{channel_id}.last_ts"


def _remaining_seconds(started: float, max_seconds: float) -> float | None:
    if not max_seconds:
        return None
    return max(0.0, max_seconds - (time.monotonic() - started))


def _is_allowlisted_notification_event(
    event: Mapping[str, Any],
    *,
    config: SlackDmConfig,
    expected_channel_id: str = "",
) -> bool:
    channel_id = str(event.get("channel") or "")
    if not channel_id or channel_id == expected_channel_id:
        return False
    if channel_id not in set(config.watched_channel_ids):
        return False
    if (
        config.watch_requires_mention
        and not _mentions_configured_user(str(event.get("text") or ""), config=config)
        and str(event.get("user") or "") != config.user_id
    ):
        return False
    return True


def _mentions_configured_user(text: str, *, config: SlackDmConfig) -> bool:
    return bool(config.user_id and f"<@{config.user_id}>" in text)


def _slack_token_kind(token: str) -> str:
    if not token:
        return "missing"
    if token.startswith("xoxb-"):
        return "bot"
    if token.startswith("xoxp-"):
        return "user"
    if token.startswith("xapp-"):
        return "app-level"
    if token.startswith("xwfp-"):
        return "workflow"
    return "unknown"

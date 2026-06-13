from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from typing import Any, Callable, Mapping, Protocol
from urllib import request as urlrequest
from urllib.parse import urlencode

from .domain import IncomingMessage, OrchestrationResult, OutboundMessage
from .orchestrator import TeamTaskOrchestrator
from .outbound_delivery import outbound_dedupe_key
from .runtime_guard import reply_instance_rejection as _reply_instance_rejection
from .store import TeamTaskStore


class SlackAdapterError(RuntimeError):
    """Raised for local Slack adapter configuration/API failures."""


SLACK_PERSONAL_DM_BOT_SCOPES = ("chat:write", "im:history", "im:write", "reactions:write")

# Maximum number of send attempts for a queued outbound message before it is
# marked terminally 'failed'. Until the cap is reached a failed send is left
# 'pending' so the next drain retries it.
MAX_OUTBOUND_SEND_ATTEMPTS = 5


class LiveChatTransport(Protocol):
    """Provider-generic outbound/inbound seam the generic queue/drain depend on.

    The generic ``drain_outbound_queue`` only needs ``send_personal`` to push a
    queued reply; ``poll_messages`` rounds out the live-runtime contract so a
    transport doubles as the inbound source.  ``SlackDmAdapter`` already
    satisfies both (``send_personal`` returns the provider ts/id string).
    """

    def poll_messages(self) -> tuple[IncomingMessage, ...]:
        """Return newly observed chat messages without mutating orchestration state."""

    def send_personal(self, actor_id: str, text: str) -> str:
        """Send text to one person's private chat; return the provider message id."""


@dataclass(frozen=True)
class SlackDmConfig:
    actor_id: str = "me"
    user_id: str = ""
    dm_channel_id: str = ""
    bot_user_id: str = ""
    bot_token: str = field(default="", repr=False)
    chat_id: str = "slack/dm"
    instance_id: str = ""
    allowed_instance_id: str = ""
    watched_channel_ids: tuple[str, ...] = ()
    watch_requires_mention: bool = True

    @classmethod
    def from_env(cls) -> "SlackDmConfig":
        return cls(
            actor_id=os.environ.get("TASK_MANAGEMENT_SLACK_ACTOR_ID", "me"),
            user_id=os.environ.get("SLACK_USER_ID", ""),
            dm_channel_id=os.environ.get("SLACK_DM_CHANNEL_ID", ""),
            bot_user_id=os.environ.get("SLACK_BOT_USER_ID", ""),
            bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            instance_id=os.environ.get("TASK_MANAGEMENT_INSTANCE_ID", ""),
            allowed_instance_id=os.environ.get("TASK_MANAGEMENT_ALLOWED_INSTANCE_ID", ""),
            watched_channel_ids=_csv_env(
                "TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS",
                "SLACK_WATCH_CHANNEL_IDS",
            ),
            watch_requires_mention=_bool_env(
                "TASK_MANAGEMENT_SLACK_WATCH_REQUIRE_MENTION",
                default=True,
            ),
        )


@dataclass(frozen=True)
class SlackLiveConfigCheck:
    ok: bool
    can_poll: bool
    can_send: bool
    actor_id: str
    token_env: str
    token_kind: str
    bot_token_set: bool
    dm_channel_id_set: bool
    user_id_set: bool
    bot_user_id_set: bool
    dm_resolution: str
    required_bot_scopes: tuple[str, ...]
    instance_id: str = ""
    allowed_instance_id: str = ""
    instance_guard_ok: bool = True
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    setup_steps: tuple[str, ...] = ()


def diagnose_slack_live_config(config: SlackDmConfig) -> SlackLiveConfigCheck:
    """Validate local Slack live Web API env without leaking secrets or calling Slack."""

    config_errors: list[str] = []
    guard_errors: list[str] = []
    warnings: list[str] = []
    setup_steps = [
        "Create/install a Slack app with a bot user.",
        "Grant bot scopes: chat:write, im:history, im:write, reactions:write.",
        "Set SLACK_BOT_TOKEN to the installed xoxb- bot token.",
        "Set SLACK_DM_CHANNEL_ID to a D... personal DM id, or set SLACK_USER_ID so conversations.open can resolve one.",
        "Set SLACK_USER_ID and enable App Home if you want the Home tab to show the task page summary.",
        "Optional: set TASK_MANAGEMENT_SLACK_WATCH_CHANNEL_IDS to a comma-separated allowlist of D/C/G channels to triage mention notifications into confirmation-only task candidates.",
        "Run slack-doctor first, then slack-fast-cycle or dogfood-loop with --send only when ready.",
    ]

    token_kind = _slack_token_kind(config.bot_token)
    if not config.bot_token:
        config_errors.append("SLACK_BOT_TOKEN is required for live Slack Web API calls.")
    elif token_kind != "bot":
        config_errors.append(
            f"SLACK_BOT_TOKEN must be a bot token beginning with xoxb- for this adapter; got {token_kind} token."
        )

    if config.dm_channel_id:
        if not is_slack_personal_dm_channel_id(config.dm_channel_id):
            config_errors.append("SLACK_DM_CHANNEL_ID must be a personal DM conversation id beginning with D.")
        dm_resolution = "SLACK_DM_CHANNEL_ID"
    elif config.user_id:
        dm_resolution = "SLACK_USER_ID via conversations.open"
        warnings.append("SLACK_DM_CHANNEL_ID is not set; first live run will call conversations.open.")
    else:
        dm_resolution = ""
        config_errors.append("Set either SLACK_DM_CHANNEL_ID or SLACK_USER_ID for the personal DM target.")

    if config.user_id and not config.user_id.startswith(("U", "W")):
        warnings.append("SLACK_USER_ID usually begins with U or W; verify it is a Slack user id.")
    if not config.bot_user_id:
        warnings.append("SLACK_BOT_USER_ID is optional, but setting it gives an extra self-message filter.")
    if config.watched_channel_ids and config.watch_requires_mention and not config.user_id:
        warnings.append("SLACK_USER_ID is required to filter allowlisted Slack watch channels to user mentions.")

    guard_error = _reply_instance_rejection(config)
    if guard_error:
        guard_errors.append(guard_error)

    can_poll = not config_errors
    can_send = can_poll and not guard_errors
    ok = can_poll and can_send
    return SlackLiveConfigCheck(
        ok=ok,
        can_poll=can_poll,
        can_send=can_send,
        actor_id=config.actor_id,
        token_env="SLACK_BOT_TOKEN",
        token_kind=token_kind,
        bot_token_set=bool(config.bot_token),
        dm_channel_id_set=bool(config.dm_channel_id),
        user_id_set=bool(config.user_id),
        bot_user_id_set=bool(config.bot_user_id),
        dm_resolution=dm_resolution,
        required_bot_scopes=SLACK_PERSONAL_DM_BOT_SCOPES,
        instance_id=config.instance_id,
        allowed_instance_id=config.allowed_instance_id,
        instance_guard_ok=not guard_errors,
        errors=tuple((*config_errors, *guard_errors)),
        warnings=tuple(warnings),
        setup_steps=tuple(setup_steps),
    )


class SlackWebClient(Protocol):
    def open_dm(self, user_id: str) -> str:
        """Return a D... DM conversation id."""

    def read_channel(self, channel_id: str, *, oldest: str = "", limit: int = 100) -> tuple[Mapping[str, Any], ...]:
        """Return Slack messages, usually newest-first from Slack Web API."""

    def send_message(self, channel_id: str, text: str) -> str:
        """Send a Slack message and return provider ts/link/id."""

    def delete_message(self, channel_id: str, ts: str) -> None:
        """Delete a bot-authored Slack message by conversation id + ts."""

    def publish_home_view(self, user_id: str, view: Mapping[str, Any]) -> str:
        """Publish a Slack App Home view for one user and return the view id/hash."""

    def add_reaction(self, channel_id: str, ts: str, name: str) -> None:
        """Add an emoji reaction to a message by conversation id + ts."""

    def remove_reaction(self, channel_id: str, ts: str, name: str) -> None:
        """Remove an emoji reaction from a message by conversation id + ts."""


class SlackHttpClient:
    """Tiny stdlib Slack Web API client kept behind the adapter boundary."""

    def __init__(self, token: str) -> None:
        if not token:
            raise SlackAdapterError("SLACK_BOT_TOKEN is required for live Slack calls")
        self.token = token

    def open_dm(self, user_id: str) -> str:
        payload = self._post_json("https://slack.com/api/conversations.open", {"users": user_id})
        channel = payload.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            raise SlackAdapterError("Slack conversations.open did not return a channel id")
        return str(channel_id)

    def read_channel(self, channel_id: str, *, oldest: str = "", limit: int = 100) -> tuple[Mapping[str, Any], ...]:
        query = {"channel": channel_id, "limit": str(limit)}
        if oldest:
            query["oldest"] = oldest
            query["inclusive"] = "false"
        payload = self._get_json(f"https://slack.com/api/conversations.history?{urlencode(query)}")
        return tuple(payload.get("messages") or ())

    def send_message(self, channel_id: str, text: str) -> str:
        payload = self._post_json("https://slack.com/api/chat.postMessage", {"channel": channel_id, "text": text})
        return str(payload.get("ts") or "")

    def delete_message(self, channel_id: str, ts: str) -> None:
        payload = self._post_json_unchecked("https://slack.com/api/chat.delete", {"channel": channel_id, "ts": ts})
        if payload.get("ok") or payload.get("error") == "message_not_found":
            return
        raise SlackAdapterError(str(payload.get("error") or payload))

    def publish_home_view(self, user_id: str, view: Mapping[str, Any]) -> str:
        payload = self._post_json(
            "https://slack.com/api/views.publish",
            {"user_id": user_id, "view": dict(view)},
        )
        published = payload.get("view") or {}
        if isinstance(published, Mapping):
            return str(published.get("id") or published.get("hash") or "")
        return ""

    def add_reaction(self, channel_id: str, ts: str, name: str) -> None:
        payload = self._post_json_unchecked(
            "https://slack.com/api/reactions.add",
            {"channel": channel_id, "timestamp": ts, "name": name},
        )
        if payload.get("ok") or payload.get("error") == "already_reacted":
            return
        raise SlackAdapterError(str(payload.get("error") or payload))

    def remove_reaction(self, channel_id: str, ts: str, name: str) -> None:
        payload = self._post_json_unchecked(
            "https://slack.com/api/reactions.remove",
            {"channel": channel_id, "timestamp": ts, "name": name},
        )
        if payload.get("ok") or payload.get("error") in {"no_reaction", "message_not_found"}:
            return
        raise SlackAdapterError(str(payload.get("error") or payload))

    def _get_json(self, url: str) -> dict[str, Any]:
        req = urlrequest.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with urlrequest.urlopen(req, timeout=20) as response:  # nosec - explicit live CLI path only
            return self._checked(json.loads(response.read().decode("utf-8")))

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._checked(self._post_json_unchecked(url, payload))

    def _post_json_unchecked(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urlrequest.urlopen(req, timeout=20) as response:  # nosec - explicit live CLI path only
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _checked(payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("ok"):
            raise SlackAdapterError(str(payload.get("error") or payload))
        return payload


@dataclass
class FakeSlackWebClient:
    messages: list[dict[str, Any]] = field(default_factory=list)
    channel_id: str = "DTEST"
    sent: list[tuple[str, str, str]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)
    home_views: list[tuple[str, Mapping[str, Any], str]] = field(default_factory=list)
    reactions_added: list[tuple[str, str, str]] = field(default_factory=list)
    reactions_removed: list[tuple[str, str, str]] = field(default_factory=list)

    def open_dm(self, user_id: str) -> str:
        return self.channel_id

    def read_channel(self, channel_id: str, *, oldest: str = "", limit: int = 100) -> tuple[Mapping[str, Any], ...]:
        oldest_value = float(oldest or 0)
        messages = [item for item in self.messages if item.get("channel", channel_id) == channel_id]
        messages = [item for item in messages if float(str(item.get("ts", "0"))) > oldest_value]
        return tuple(sorted(messages, key=lambda item: str(item.get("ts", "")), reverse=True)[:limit])

    def send_message(self, channel_id: str, text: str) -> str:
        ts = f"2000.{len(self.sent) + 1:06d}"
        self.sent.append((channel_id, ts, text))
        return ts

    def delete_message(self, channel_id: str, ts: str) -> None:
        self.deleted.append((channel_id, ts))

    def publish_home_view(self, user_id: str, view: Mapping[str, Any]) -> str:
        view_id = f"VHOME{len(self.home_views) + 1:06d}"
        self.home_views.append((user_id, view, view_id))
        return view_id

    def add_reaction(self, channel_id: str, ts: str, name: str) -> None:
        self.reactions_added.append((channel_id, ts, name))

    def remove_reaction(self, channel_id: str, ts: str, name: str) -> None:
        self.reactions_removed.append((channel_id, ts, name))


class SlackDmAdapter:
    def __init__(self, config: SlackDmConfig, client: SlackWebClient | None = None, *, oldest: str = "") -> None:
        self.config = config
        self.client = client or SlackHttpClient(config.bot_token)
        self.oldest = oldest
        self._channel_id = config.dm_channel_id

    @property
    def channel_id(self) -> str:
        if not self._channel_id:
            if not self.config.user_id:
                raise SlackAdapterError("SLACK_DM_CHANNEL_ID or SLACK_USER_ID is required")
            self._channel_id = self.client.open_dm(self.config.user_id)
        if not is_slack_personal_dm_channel_id(self._channel_id):
            raise SlackAdapterError(
                "Slack DM adapter only supports personal D... conversation ids; "
                f"got {self._channel_id!r}. Set SLACK_DM_CHANNEL_ID to a personal DM or use SLACK_USER_ID."
            )
        return self._channel_id

    def poll_messages(self) -> tuple[IncomingMessage, ...]:
        messages = self.client.read_channel(self.channel_id, oldest=self.oldest)
        normalized = [
            item
            for item in (
                slack_message_to_incoming(raw, config=self.config, channel_id=self.channel_id)
                for raw in messages
            )
            if item is not None
        ]
        return tuple(sorted(normalized, key=lambda item: item.received_at))

    def send_personal(self, actor_id: str, text: str) -> str:
        instance_rejection = _reply_instance_rejection(self.config)
        if instance_rejection:
            raise SlackAdapterError(instance_rejection)
        if actor_id != self.config.actor_id:
            raise SlackAdapterError(f"Slack DM adapter only supports actor {self.config.actor_id!r}; got {actor_id!r}")
        channel_id = self.channel_id
        if not channel_id.startswith("D"):
            raise SlackAdapterError(
                "Slack personal DM sends require a D... conversation id; "
                f"got {channel_id!r}. Set SLACK_DM_CHANNEL_ID to a personal DM or use SLACK_USER_ID."
            )
        return self.client.send_message(channel_id, text)

    def send_family(self, text: str) -> None:
        raise SlackAdapterError("team-room Slack sending is out of scope for the personal DM MVP")

    def publish_home(self, user_id: str, view: Mapping[str, Any]) -> str:
        instance_rejection = _reply_instance_rejection(self.config)
        if instance_rejection:
            raise SlackAdapterError(instance_rejection)
        if not user_id:
            raise SlackAdapterError("Slack App Home publish requires SLACK_USER_ID or app_home_opened event.user")
        if self.config.user_id and user_id != self.config.user_id:
            raise SlackAdapterError(
                f"Slack App Home publish is restricted to configured user {self.config.user_id!r}; got {user_id!r}."
            )
        return self.client.publish_home_view(user_id, view)

    def add_reaction(self, channel_id: str, ts: str, name: str) -> None:
        self.client.add_reaction(channel_id, ts, name)

    def remove_reaction(self, channel_id: str, ts: str, name: str) -> None:
        self.client.remove_reaction(channel_id, ts, name)


def slack_message_to_incoming(
    message: Mapping[str, Any],
    *,
    config: SlackDmConfig,
    channel_id: str,
    visibility: str = "private",
) -> IncomingMessage | None:
    text = str(message.get("text") or "").strip()
    ts = str(message.get("ts") or "")
    user = str(message.get("user") or "")
    if not text or not ts:
        return None
    if message.get("subtype") or message.get("bot_id") or (config.bot_user_id and user == config.bot_user_id):
        return None
    received_at = (
        datetime.fromisoformat(str(message["received_at"]))
        if message.get("received_at")
        else datetime.fromtimestamp(float(ts.split(".")[0]))
    )
    return IncomingMessage(
        message_id=f"slack/{channel_id}/{ts}",
        sender_id=config.actor_id,
        chat_id=channel_id,
        visibility=visibility,  # type: ignore[arg-type]
        text=text,
        received_at=received_at,
    )


@dataclass(frozen=True)
class SlackPollResult:
    messages: tuple[IncomingMessage, ...]
    results: tuple[OrchestrationResult, ...]
    outbound_messages: tuple[OutboundMessage, ...]


def run_slack_dm_once(
    *,
    store: TeamTaskStore,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    send: bool,
    now: datetime,
) -> SlackPollResult:
    messages = adapter.poll_messages()
    results: list[OrchestrationResult] = []
    outbound: list[OutboundMessage] = []
    latest_ts = store.get_integration_state(_last_ts_key(adapter.config.actor_id)) or adapter.oldest
    for message in messages:
        store.append_event("slack.message.polled", {"message_id": message.message_id}, occurred_at=message.received_at)
        result = orchestrator.handle_message(message)
        results.append(result)
        outbound.extend(result.outbound_messages)
        if send and result.outbound_messages:
            # Salt the fallback dedupe key with the triggering inbound id so two
            # distinct inbound messages that reduce to the same key are both
            # delivered, while re-polling the same inbound stays deduped.
            queue_slack_outbound(
                store,
                adapter,
                tuple(result.outbound_messages),
                queued_at=now,
                source_message_id=message.message_id,
            )
        ts = message.message_id.rsplit("/", 1)[-1]
        if not latest_ts or float(ts) > float(latest_ts):
            latest_ts = ts
    if send and outbound:
        # Drain (which sends via the Slack Web API) BEFORE advancing last_ts so a
        # send-side failure raises here and leaves the inbound messages
        # re-pollable. Inbound dedupe via has_message still prevents reprocessing
        # already-handled messages once last_ts advances on the next clean cycle.
        drain_slack_outbound_queue(store, adapter, sent_at=now, raise_on_error=True)
    if latest_ts:
        store.set_integration_state(_last_ts_key(adapter.config.actor_id), latest_ts, updated_at=now)
    return SlackPollResult(messages=messages, results=tuple(results), outbound_messages=tuple(outbound))


def dispatch_slack_outbound(
    store: TeamTaskStore,
    adapter: SlackDmAdapter,
    messages: tuple[OutboundMessage, ...],
    *,
    sent_at: datetime,
) -> None:
    queue_slack_outbound(store, adapter, messages, queued_at=sent_at)
    drain_slack_outbound_queue(store, adapter, sent_at=sent_at, raise_on_error=True)


# A delivery resolver maps an OutboundMessage to the (recipient_id, text) a
# provider will actually send, or None to drop the message entirely.  Slack's
# resolver folds team_room replies onto the personal DM actor (see
# ``_slack_delivery``); other providers supply their own.
DeliveryResolver = Callable[[OutboundMessage], "tuple[str, str] | None"]


def queue_outbound(
    store: TeamTaskStore,
    transport: LiveChatTransport,
    messages: tuple[OutboundMessage, ...],
    *,
    provider: str,
    queued_at: datetime,
    source_message_id: str | None = None,
    actor_id: str,
    resolve_delivery: DeliveryResolver,
    delivery_surface: str,
) -> int:
    """Provider-generic enqueue core for outbound replies.

    Event names are derived as ``f"{provider}.message.{queued|skipped}"`` and the
    dedupe key uses the matching ``{provider}-outbound/`` prefix, so passing
    ``provider="slack"`` reproduces every historical Slack event/key byte-for-byte.
    """

    queued = 0
    for message in messages:
        delivery = resolve_delivery(message)
        if delivery is None:
            continue
        recipient_id, text = delivery
        if recipient_id != actor_id:
            store.append_event(
                f"{provider}.message.skipped",
                {
                    "reason": "unsupported_recipient",
                    "recipient_id": recipient_id,
                    "message_type": message.message_type,
                    "proposal_id": message.proposal_id,
                    "approval_request_id": message.approval_request_id,
                },
                occurred_at=queued_at,
            )
            continue
        dedupe_key = outbound_dedupe_key(
            message,
            recipient_id=recipient_id,
            source_message_id=source_message_id,
            provider=provider,
        )
        if store.has_outbound_delivery(dedupe_key):
            store.append_event(
                f"{provider}.message.skipped",
                {
                    "reason": "duplicate_dedupe_key",
                    "dedupe_key": dedupe_key,
                    "message_type": message.message_type,
                    "proposal_id": message.proposal_id,
                    "approval_request_id": message.approval_request_id,
                    "recipient_id": recipient_id,
                },
                occurred_at=queued_at,
            )
            continue
        inserted = store.enqueue_outbound_message(
            dedupe_key=dedupe_key,
            provider=provider,
            surface=message.surface,
            recipient_id=recipient_id,
            message_type=message.message_type,
            proposal_id=message.proposal_id,
            approval_request_id=message.approval_request_id,
            text=text,
            message=asdict(message),
            queued_at=queued_at,
        )
        if inserted:
            queued += 1
            store.append_event(
                f"{provider}.message.queued",
                {
                    "dedupe_key": dedupe_key,
                    "message": message,
                    "original_surface": message.surface,
                    "delivery_surface": delivery_surface,
                    "recipient_id": recipient_id,
                },
                occurred_at=queued_at,
            )
    return queued


def drain_outbound_queue(
    store: TeamTaskStore,
    transport: LiveChatTransport,
    *,
    provider: str,
    sent_at: datetime,
    limit: int = 100,
    raise_on_error: bool = False,
    actor_id: str,
    delivery_surface: str,
) -> int:
    """Provider-generic drain core: send each pending row via ``transport``.

    Event names are derived as ``f"{provider}.message.{skipped|failed|sent}"``.
    The A2 retry cap (``MAX_OUTBOUND_SEND_ATTEMPTS``) and the leave-pending-on-
    transient-failure behavior are reached identically for every provider.
    """

    sent = 0
    for item in store.list_pending_outbound_messages(provider=provider, limit=limit):
        dedupe_key = str(item["dedupe_key"])
        recipient_id = str(item["recipient_id"])
        text = str(item["text"])
        message = _outbound_message_from_queue(item["message"])
        if recipient_id != actor_id:
            store.mark_outbound_message(
                dedupe_key,
                status="skipped",
                updated_at=sent_at,
                last_error="unsupported_recipient",
            )
            store.append_event(
                f"{provider}.message.skipped",
                {
                    "reason": "unsupported_recipient",
                    "recipient_id": recipient_id,
                    "message_type": item["message_type"],
                    "proposal_id": item["proposal_id"],
                    "approval_request_id": item["approval_request_id"],
                },
                occurred_at=sent_at,
            )
            continue
        if store.has_outbound_delivery(dedupe_key):
            store.mark_outbound_message(dedupe_key, status="sent", updated_at=sent_at)
            continue
        try:
            provider_message_id = transport.send_personal(recipient_id, text)
        except Exception as exc:
            attempts = int(item.get("attempts") or 0) + 1
            retryable = attempts < MAX_OUTBOUND_SEND_ATTEMPTS
            store.mark_outbound_message(
                dedupe_key,
                status="pending" if retryable else "failed",
                updated_at=sent_at,
                last_error=str(exc),
                increment_attempts=True,
            )
            store.append_event(
                f"{provider}.message.failed",
                {
                    "dedupe_key": dedupe_key,
                    "message_type": item["message_type"],
                    "proposal_id": item["proposal_id"],
                    "approval_request_id": item["approval_request_id"],
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "attempts": attempts,
                    "retryable": retryable,
                },
                occurred_at=sent_at,
            )
            if raise_on_error:
                raise
            continue
        store.record_outbound_delivery(
            dedupe_key=dedupe_key,
            surface=message.surface,
            recipient_id=recipient_id,
            provider=provider,
            provider_message_id=provider_message_id,
            sent_at=sent_at,
            payload={
                "text": text,
                "original_surface": message.surface,
                "delivery_surface": delivery_surface,
                "card": message.card,
            },
        )
        store.mark_outbound_message(
            dedupe_key,
            status="sent",
            provider_message_id=provider_message_id,
            updated_at=sent_at,
            increment_attempts=True,
        )
        store.append_event(
            f"{provider}.message.sent",
            {
                "dedupe_key": dedupe_key,
                "message": message,
                "original_surface": message.surface,
                "delivery_surface": delivery_surface,
                "recipient_id": recipient_id,
            },
            occurred_at=sent_at,
        )
        sent += 1
    return sent


def queue_slack_outbound(
    store: TeamTaskStore,
    adapter: SlackDmAdapter,
    messages: tuple[OutboundMessage, ...],
    *,
    queued_at: datetime,
    source_message_id: str | None = None,
) -> int:
    """Thin Slack wrapper over ``queue_outbound`` (provider="slack").

    Produces the exact ``slack.message.{queued,skipped}`` events and
    ``slack-outbound/`` dedupe keys it always has.
    """

    return queue_outbound(
        store,
        adapter,
        messages,
        provider="slack",
        queued_at=queued_at,
        source_message_id=source_message_id,
        actor_id=adapter.config.actor_id,
        resolve_delivery=lambda message: _slack_delivery(message, adapter=adapter),
        delivery_surface="slack_personal_dm",
    )


def drain_slack_outbound_queue(
    store: TeamTaskStore,
    adapter: SlackDmAdapter,
    *,
    sent_at: datetime,
    limit: int = 100,
    raise_on_error: bool = False,
) -> int:
    """Thin Slack wrapper over ``drain_outbound_queue`` (provider="slack").

    Produces the exact ``slack.message.{skipped,failed,sent}`` events and keeps
    the A2 retry cap and A3/A4 behavior identical to today.
    """

    return drain_outbound_queue(
        store,
        adapter,
        provider="slack",
        sent_at=sent_at,
        limit=limit,
        raise_on_error=raise_on_error,
        actor_id=adapter.config.actor_id,
        delivery_surface="slack_personal_dm",
    )


def _slack_delivery(message: OutboundMessage, *, adapter: SlackDmAdapter) -> tuple[str, str] | None:
    recipient_id = message.recipient_id
    text = message.text
    if message.surface == "team_room":
        recipient_id = adapter.config.actor_id
        text = message.text
    elif message.surface != "personal_chat":
        return None
    return recipient_id, text


def _outbound_message_from_queue(payload: Mapping[str, Any]) -> OutboundMessage:
    return OutboundMessage(
        surface=str(payload.get("surface") or "personal_chat"),  # type: ignore[arg-type]
        recipient_id=str(payload.get("recipient_id") or ""),
        message_type=str(payload.get("message_type") or ""),
        text=str(payload.get("text") or ""),
        proposal_id=str(payload.get("proposal_id") or ""),
        approval_request_id=str(payload.get("approval_request_id") or ""),
        card={str(k): str(v) for k, v in dict(payload.get("card") or {}).items()},
    )


def _last_ts_key(actor_id: str) -> str:
    return f"slack.dm.{actor_id}.last_ts"


def _outbound_dedupe_key(
    message: OutboundMessage,
    *,
    recipient_id: str | None = None,
    source_message_id: str | None = None,
) -> str:
    # The dedupe-key format now lives in outbound_delivery; this stays as the
    # slack_adapter-local name so existing call sites and references are intact.
    return outbound_dedupe_key(
        message, recipient_id=recipient_id, source_message_id=source_message_id
    )


def is_slack_personal_dm_channel_id(channel_id: str) -> bool:
    return channel_id.startswith("D")


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


def _csv_env(*names: str) -> tuple[str, ...]:
    for name in names:
        raw = os.environ.get(name, "")
        if raw.strip():
            return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    return ()


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}

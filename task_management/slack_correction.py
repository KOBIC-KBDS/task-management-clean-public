from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib

from .slack_adapter import SlackDmAdapter
from .store import TeamTaskStore


@dataclass(frozen=True)
class SlackCorrectionResult:
    channel_id: str
    deleted_ts: tuple[str, ...]
    sent: bool
    provider_message_id: str
    dedupe_key: str
    text_hash: str


def recall_and_send_slack_correction(
    *,
    store: TeamTaskStore,
    adapter: SlackDmAdapter,
    delete_ts: tuple[str, ...],
    correction_text: str,
    actor_id: str,
    reason: str,
    dedupe_key: str = "",
    verify_contains: tuple[str, ...] = (),
    force: bool = False,
    sent_at: datetime | None = None,
) -> SlackCorrectionResult:
    """Delete bad bot replies and send one UTF-8 correction DM.

    The caller should prefer ``--text-file`` / file input for Korean text. That
    avoids Windows PowerShell pipeline encoding from turning a valid correction
    into mojibake before the Slack API call.
    """

    now = sent_at or datetime.now().replace(microsecond=0)
    text = correction_text.strip()
    if not text:
        raise ValueError("correction_text must not be empty")
    missing = [needle for needle in verify_contains if needle not in text]
    if missing:
        raise ValueError(f"correction_text is missing required verify text: {missing}")

    channel_id = adapter.channel_id
    text_hash = _text_hash(text)
    stable_dedupe_key = dedupe_key or f"slack-correction/{channel_id}/{text_hash}"

    deleted: list[str] = []
    for ts in delete_ts:
        clean_ts = ts.strip()
        if not clean_ts:
            continue
        adapter.client.delete_message(channel_id, clean_ts)
        deleted.append(clean_ts)
        store.append_event(
            "slack.message.deleted",
            {
                "channel": channel_id,
                "ts": clean_ts,
                "reason": reason,
            },
            occurred_at=now,
        )

    if store.has_outbound_delivery(stable_dedupe_key) and not force:
        return SlackCorrectionResult(
            channel_id=channel_id,
            deleted_ts=tuple(deleted),
            sent=False,
            provider_message_id="",
            dedupe_key=stable_dedupe_key,
            text_hash=text_hash,
        )

    provider_message_id = adapter.send_personal(actor_id, text)
    store.record_outbound_delivery(
        dedupe_key=stable_dedupe_key,
        surface="personal_chat",
        recipient_id=actor_id,
        provider="slack",
        provider_message_id=provider_message_id,
        sent_at=now,
        payload={
            "text": text,
            "delivery_surface": "slack_personal_dm",
            "message_type": "manual_context_correction",
            "deleted_ts": list(deleted),
            "reason": reason,
        },
    )
    store.append_event(
        "slack.message.sent",
        {
            "dedupe_key": stable_dedupe_key,
            "provider_message_id": provider_message_id,
            "delivery_surface": "slack_personal_dm",
            "recipient_id": actor_id,
            "message_type": "manual_context_correction",
            "text_hash": text_hash,
        },
        occurred_at=now,
    )
    return SlackCorrectionResult(
        channel_id=channel_id,
        deleted_ts=tuple(deleted),
        sent=True,
        provider_message_id=provider_message_id,
        dedupe_key=stable_dedupe_key,
        text_hash=text_hash,
    )


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

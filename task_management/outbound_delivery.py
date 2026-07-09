"""Single home for outbound reserve/dedupe-key behavior.

Two concerns lived as copy-paste across the secretary, reminder, pending-info,
and CLI code paths:

* the reserve sequence ``record_outbound_delivery`` -> (bail if already
  recorded) -> ``append_event``, and
* the ``slack-outbound/...`` dedupe-key *format*, which was minted privately in
  ``slack_adapter`` and hand-rebuilt elsewhere.

This module owns both.  It is a leaf over ``domain`` and ``store`` so adapters
can import the public dedupe-key helpers without creating an import cycle.

INVARIANT 7 (bidirectional dedup) depends on these producing byte-identical
``dedupe_key`` strings, the constant provider ``"slack"``, and the same recorded
events as the inlined copies they replace.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .domain import OutboundMessage
from .store import TeamTaskStore


OUTBOUND_KEY_PREFIX = "slack-outbound"


def reserve_outbound(
    store: TeamTaskStore,
    message: OutboundMessage,
    *,
    dedupe_key: str,
    provider_message_id: str,
    event_type: str,
    event_payload: dict[str, Any],
    now: datetime,
) -> bool:
    """Record the single outbound delivery for ``message`` and log its event.

    Returns ``True`` when the delivery was newly recorded (caller should keep the
    message) and ``False`` when an identical ``dedupe_key`` was already reserved
    (caller should skip).  Mirrors the historical inline sequence exactly: the
    provider is always ``"slack"``, the payload is the message text/card, and the
    event is appended only after a fresh reservation.
    """

    recorded = store.record_outbound_delivery(
        dedupe_key=dedupe_key,
        surface=message.surface,
        recipient_id=message.recipient_id,
        provider="slack",
        provider_message_id=provider_message_id,
        sent_at=now,
        payload={"text": message.text, "card": message.card},
    )
    if not recorded:
        return False
    store.append_event(event_type, event_payload, occurred_at=now)
    return True


def outbound_dedupe_key(
    message: OutboundMessage,
    *,
    recipient_id: str | None = None,
    source_message_id: str | None = None,
    provider: str = "slack",
) -> str:
    """Public outbound dedupe-key format (was slack_adapter._outbound_dedupe_key).

    An explicit ``card['dedupe_key']`` wins.  Otherwise the key is
    ``{provider}-outbound/{recipient}/{message_type}/{stable}`` where ``stable``
    is the approval-request id, then proposal id, then the text.  A
    ``source_message_id`` salts the fallback key so two distinct inbound messages
    that reduce to the same stable key are not collapsed, while re-polling the
    same inbound stays idempotent.

    ``provider`` defaults to ``"slack"`` so every historical Slack key string is
    produced byte-identically (prefix ``slack-outbound/``); only non-Slack
    providers see a different prefix.
    """

    explicit = message.card.get("dedupe_key")
    if explicit:
        return explicit
    stable = message.approval_request_id or message.proposal_id or message.text
    key = f"{provider}-outbound/{recipient_id or message.recipient_id}/{message.message_type}/{stable}"
    if source_message_id:
        key = f"{key}/{source_message_id}"
    return key


def approval_request_delivery_key(approver_id: str, request_id: str) -> str:
    """Base outbound key for an approval-request prompt.

    Equivalent to the fallback ``outbound_dedupe_key`` produces for an
    approval-request message: ``slack-outbound/{approver}/approval_request/{id}``.
    Inbound-triggered prompts salt this with ``/{source_message_id}`` (see
    ``outbound_dedupe_key``); callers reconstruct the salted variant themselves.
    """

    return f"{OUTBOUND_KEY_PREFIX}/{approver_id}/approval_request/{request_id}"

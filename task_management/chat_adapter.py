from __future__ import annotations

from .domain import OutboundMessage
from .slack_adapter import LiveChatTransport

# ChatAdapter was a forward-looking protocol that the real SlackDmAdapter never
# satisfied (send_personal returns the provider message id, not None; the
# adapter has send_family, not send_team).  The channel-extensibility seam now
# has exactly one outbound transport protocol -- LiveChatTransport -- which
# SlackDmAdapter does satisfy.  ChatAdapter is folded into it; this name is kept
# as an alias so existing references resolve to the single survivor.
ChatAdapter = LiveChatTransport


def dispatch_outbound(adapter: LiveChatTransport, messages: tuple[OutboundMessage, ...]) -> None:
    """Route outbound replies through a LiveChatTransport.

    Only personal-chat surfaces are deliverable through the generic transport
    (send_personal); other surfaces are folded onto the personal DM by the
    provider's queue/drain layer before they reach a transport, so nothing is
    routed here for them.
    """

    for message in messages:
        if message.surface == "personal_chat":
            adapter.send_personal(message.recipient_id, message.text)

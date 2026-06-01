from __future__ import annotations

from typing import Protocol

from .domain import IncomingMessage, OutboundMessage


class ChatAdapter(Protocol):
    """Boundary for future KakaoTalk, Telegram, or simulator adapters."""

    def poll_messages(self) -> tuple[IncomingMessage, ...]:
        """Return newly observed chat messages without changing orchestration state."""

    def send_personal(self, actor_id: str, text: str) -> None:
        """Send text to one person's private chat."""

    def send_team(self, text: str) -> None:
        """Send text to the team room."""


def dispatch_outbound(adapter: ChatAdapter, messages: tuple[OutboundMessage, ...]) -> None:
    for message in messages:
        if message.surface == "personal_chat":
            adapter.send_personal(message.recipient_id, message.text)
        elif message.surface == "team_room":
            adapter.send_team(message.text)

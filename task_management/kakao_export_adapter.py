from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import re

from .domain import IncomingMessage, MessageVisibility


DATE_HEADER_RE = re.compile(r"^-+\s*(?P<year>\d{4})년\s*(?P<month>\d{1,2})월\s*(?P<day>\d{1,2})일.*-+$")
BRACKET_MESSAGE_RE = re.compile(
    r"^\[(?P<sender>[^\]]+)]\s*\[(?P<ampm>오전|오후)\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})]\s*(?P<text>.*)$"
)
COMMA_MESSAGE_RE = re.compile(
    r"^(?P<year>\d{4})\.\s*(?P<month>\d{1,2})\.\s*(?P<day>\d{1,2})\.\s*"
    r"(?P<ampm>오전|오후)\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}),\s*"
    r"(?P<sender>[^:：]+)\s*[:：]\s*(?P<text>.*)$"
)


def parse_kakao_text_export(
    text: str,
    *,
    actor_aliases: dict[str, str] | None = None,
    chat_id: str = "kakao/team",
    visibility: MessageVisibility = "team",
    default_date: date | None = None,
) -> tuple[IncomingMessage, ...]:
    """Parse a local KakaoTalk text export into IncomingMessage records."""

    aliases = actor_aliases or {}
    current_date = default_date or date.today()
    messages: list[IncomingMessage] = []
    pending: dict[str, object] | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line:
            continue

        date_match = DATE_HEADER_RE.match(line)
        if date_match:
            _flush_pending(messages, pending, chat_id=chat_id, visibility=visibility)
            pending = None
            current_date = date(
                int(date_match.group("year")),
                int(date_match.group("month")),
                int(date_match.group("day")),
            )
            continue

        comma_match = COMMA_MESSAGE_RE.match(line)
        bracket_match = BRACKET_MESSAGE_RE.match(line)
        match = comma_match or bracket_match
        if match:
            _flush_pending(messages, pending, chat_id=chat_id, visibility=visibility)
            msg_date = current_date
            if comma_match:
                msg_date = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            sender_name = match.group("sender").strip()
            pending = {
                "line_number": line_number,
                "sender_id": aliases.get(sender_name, sender_name),
                "text": match.group("text").strip(),
                "received_at": datetime.combine(
                    msg_date,
                    _parse_korean_time(
                        match.group("ampm"),
                        int(match.group("hour")),
                        int(match.group("minute")),
                    ),
                ),
            }
            continue

        if pending is not None:
            pending["text"] = f"{pending['text']}\n{line.strip()}".strip()

    _flush_pending(messages, pending, chat_id=chat_id, visibility=visibility)
    return tuple(messages)


def _flush_pending(
    messages: list[IncomingMessage],
    pending: dict[str, object] | None,
    *,
    chat_id: str,
    visibility: MessageVisibility,
) -> None:
    if pending is None:
        return
    sender_id = str(pending["sender_id"])
    text = str(pending["text"]).strip()
    received_at = pending["received_at"]
    if not text or not isinstance(received_at, datetime):
        return
    messages.append(
        IncomingMessage(
            message_id=_message_id(chat_id, int(pending["line_number"]), sender_id, text, received_at),
            sender_id=sender_id,
            chat_id=chat_id,
            visibility=visibility,
            text=text,
            received_at=received_at,
        )
    )


def _parse_korean_time(ampm: str, hour: int, minute: int) -> time:
    normalized_hour = hour % 12
    if ampm == "오후":
        normalized_hour += 12
    return time(normalized_hour, minute)


def _message_id(chat_id: str, line_number: int, sender_id: str, text: str, received_at: datetime) -> str:
    digest_source = f"{chat_id}:{line_number}:{sender_id}:{received_at.isoformat()}:{text}"
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]
    return f"kakao/{digest}"

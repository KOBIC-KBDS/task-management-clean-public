"""Single home for message-id parsing and content hashing.

Centralizes the ``slack/<channel>/<ts>`` message-id format and the canonical
content hash so that a future channel (or a hash change) is a one-file edit.
"""

from __future__ import annotations

from .relations import (
    SOURCE_CHANNEL_KEY,
    SOURCE_PROVIDER_KEY,
    SOURCE_TS_KEY,
)

import hashlib

SLACK_MESSAGE_PREFIX = "slack/"


def parse_message_source(message_id: str) -> dict[str, str]:
    """Parse a ``slack/<channel>/<ts>`` message id into source-metadata fields.

    Returns ``{}`` for non-slack ids, ``{"source_provider": "slack"}`` when the
    channel/ts segments are missing, and the full provider/channel/ts mapping
    otherwise. Dict contents and insertion order are part of the contract.
    """

    if not message_id.startswith(SLACK_MESSAGE_PREFIX):
        return {}
    parts = message_id.split("/")
    if len(parts) < 3:
        return {SOURCE_PROVIDER_KEY: "slack"}
    return {
        SOURCE_PROVIDER_KEY: "slack",
        SOURCE_CHANNEL_KEY: parts[1],
        SOURCE_TS_KEY: parts[2],
    }


def source_metadata(message_id: str) -> dict[str, str]:
    """Backward-compatible alias for :func:`parse_message_source`."""

    return parse_message_source(message_id)


def text_hash(text: str) -> str:
    """Return the canonical ``sha256:<hexdigest>`` content hash for ``text``."""

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"

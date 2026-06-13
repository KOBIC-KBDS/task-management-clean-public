"""Declarative registry of message-id channel semantics.

Each :class:`ChannelSpec` lets a channel *declare* how its message ids drive the
load-bearing safety gates (intake-confirmation policy, reconciler-fallback
suppression, and the confirmation-required notification label) instead of every
gate re-hardcoding a ``startswith("slack/")`` check.

The message-id format itself is parsed by
:func:`task_management.source_refs.parse_message_source`; this module only owns
the per-channel policy semantics keyed off the id prefix / source provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from .source_refs import SLACK_MESSAGE_PREFIX


@dataclass(frozen=True)
class ChannelSpec:
    """Declared message-id semantics for one inbound channel.

    Fields map one-to-one onto the three safety gates:

    - ``team_messages_confirmation_required`` — when ``True``, a team-visibility
      message from this channel forces intake ``user_confirmation_required``
      (proposal_builder.intake_metadata).
    - ``notification_fallback_candidate`` — when ``True``, a team-visibility
      message from this channel is treated as a notification candidate and
      *suppresses* the reconciler state-linked fallback
      (task_reconciler._should_run_state_linked_fallback).
    - ``notification_label`` — the human label used in the confirmation-required
      sentence (approval_policy.render_confirmation_required_sentence).
    """

    provider: str
    id_prefix: str
    team_messages_confirmation_required: bool
    notification_fallback_candidate: bool
    notification_label: str


SLACK_CHANNEL = ChannelSpec(
    provider="slack",
    id_prefix=SLACK_MESSAGE_PREFIX,
    team_messages_confirmation_required=True,
    notification_fallback_candidate=True,
    notification_label="Slack 알림",
)


CHANNEL_REGISTRY: tuple[ChannelSpec, ...] = (SLACK_CHANNEL,)


# The non-channel default reproduces today's behavior for any message id that
# does not match a registered channel: no slack source metadata, not forced to
# confirmation-required, not a notification fallback candidate, and the generic
# external label.
DEFAULT_NOTIFICATION_LABEL = "외부 알림"


def channel_for_message_id(message_id: str) -> ChannelSpec | None:
    """Return the channel spec whose id prefix matches ``message_id``, else ``None``.

    ``None`` signals the non-channel default (the historical non-slack behavior):
    callers must not force confirmation-required, must not treat the message as a
    notification fallback candidate, and must use :data:`DEFAULT_NOTIFICATION_LABEL`.
    """

    for spec in CHANNEL_REGISTRY:
        if message_id.startswith(spec.id_prefix):
            return spec
    return None


def channel_for_provider(provider: str) -> ChannelSpec | None:
    """Return the channel spec for a ``source_provider`` metadata value, else ``None``."""

    if not provider:
        return None
    for spec in CHANNEL_REGISTRY:
        if spec.provider == provider:
            return spec
    return None


def notification_label_for_provider(provider: str) -> str:
    """Return the confirmation-required label for a ``source_provider`` metadata value.

    Falls back to :data:`DEFAULT_NOTIFICATION_LABEL` for unknown/empty providers.
    """

    spec = channel_for_provider(provider)
    return spec.notification_label if spec is not None else DEFAULT_NOTIFICATION_LABEL

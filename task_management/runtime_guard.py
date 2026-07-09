"""Channel-agnostic runtime instance guard.

A live chat runtime may only reply when its configured
``TASK_MANAGEMENT_INSTANCE_ID`` matches the ``TASK_MANAGEMENT_ALLOWED_INSTANCE_ID``
the operator pinned.  This guard reads only those two channel-agnostic values
(carried on any adapter config that exposes ``instance_id`` /
``allowed_instance_id``) so the same gate protects Slack today and any future
provider wrapper without duplicating the message strings.

The exact rejection-message strings here are part of INVARIANT 7's observable
behavior and must not change for the Slack path.
"""

from __future__ import annotations

from typing import Protocol


class InstanceGuardConfig(Protocol):
    instance_id: str
    allowed_instance_id: str


def reply_instance_rejection(config: InstanceGuardConfig) -> str:
    """Return a rejection reason when this runtime may not reply, else ``""``."""

    if not config.allowed_instance_id:
        return ""
    if not config.instance_id:
        return (
            "TASK_MANAGEMENT_ALLOWED_INSTANCE_ID is set, but TASK_MANAGEMENT_INSTANCE_ID is missing; "
            "live Slack replies are disabled for this runtime."
        )
    if config.instance_id != config.allowed_instance_id:
        return (
            "Live Slack replies are disabled for runtime instance "
            f"{config.instance_id!r}; expected {config.allowed_instance_id!r}."
        )
    return ""

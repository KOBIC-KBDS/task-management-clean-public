from __future__ import annotations

import os
from typing import Callable

from .commands import InstanceProbeCommand
from .domain import IncomingMessage, OrchestrationResult, OutboundMessage
from .store import TeamTaskStore


def _assignee_from_text(text: str) -> str:
    normalized = text.strip().lower()
    if normalized in {"나", "내가", "저", "엄마", "me", "self"}:
        return "me"
    if normalized in {"팀원", "팀원", "남편", "아내", "teammate"}:
        return "teammate"
    if normalized in {"공동", "같이", "우리", "shared"}:
        return "shared"
    if normalized in {"미정", "없음", "unassigned"}:
        return "unassigned"
    return ""


def handle_instance_probe(
    probe: InstanceProbeCommand,
    message: IncomingMessage,
    *,
    store: TeamTaskStore,
) -> OrchestrationResult:
    instance_id = os.environ.get("TASK_MANAGEMENT_INSTANCE_ID", "")
    if instance_id != probe.target_instance_id:
        store.append_event(
            "instance_probe.ignored",
            {
                "target_instance_id": probe.target_instance_id,
                "configured_instance_id": instance_id,
                "nonce": probe.nonce,
            },
            occurred_at=message.received_at,
        )
        return OrchestrationResult()

    store.append_event(
        "instance_probe.accepted",
        {
            "target_instance_id": probe.target_instance_id,
            "configured_instance_id": instance_id,
            "nonce": probe.nonce,
        },
        occurred_at=message.received_at,
    )
    return OrchestrationResult(
        outbound_messages=(
            OutboundMessage(
                surface="personal_chat",
                recipient_id=message.sender_id,
                message_type="instance_probe",
                text=(
                    f"INSTANCE_OK {probe.nonce}\n"
                    f"instance_id={instance_id}\n"
                    "이 응답은 지정된 인스턴스에서만 전송되도록 설정된 테스트 응답입니다."
                ),
                card={
                    "dedupe_key": f"instance-probe/{instance_id}/{probe.nonce}",
                    "target_instance_id": probe.target_instance_id,
                    "instance_id": instance_id,
                },
            ),
        )
    )


def handle_command_message(
    command,
    message: IncomingMessage,
    *,
    store: TeamTaskStore,
    handle_approval: Callable[..., OrchestrationResult],
    handle_change: Callable[..., OrchestrationResult],
    handle_assign: Callable[..., OrchestrationResult],
    handle_complete: Callable[..., OrchestrationResult],
) -> OrchestrationResult:
    if command.action in {"accept", "reject"}:
        return handle_approval(
            request_id=command.target_id,
            approver_id=message.sender_id,
            accepted=command.action == "accept",
            decided_at=message.received_at,
        )
    if command.action == "change":
        return handle_change(
            target_id=command.target_id,
            actor_id=message.sender_id,
            body=command.body,
            changed_at=message.received_at,
        )
    if command.action == "assign":
        return handle_assign(
            target_id=command.target_id,
            actor_id=message.sender_id,
            body=command.body,
            assigned_at=message.received_at,
        )
    if command.action == "complete":
        return handle_complete(
            target_id=command.target_id,
            actor_id=message.sender_id,
            completed_at=message.received_at,
        )
    return OrchestrationResult(
        outbound_messages=(
            OutboundMessage(
                surface="personal_chat",
                recipient_id=message.sender_id,
                message_type="command_not_supported",
                text=f"아직 지원하지 않는 명령입니다: {message.text}",
                card={
                    "action": command.action,
                    "target_id": command.target_id,
                    "body": command.body,
                },
            ),
        )
    )

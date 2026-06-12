from __future__ import annotations

import hashlib

from .domain import (
    ASSIGNEE_VALUES,
    PROPOSAL_KIND_VALUES,
    TeamTaskTaskCandidate,
    IncomingMessage,
    Proposal,
    ProposalKind,
)
from .slot_validator import missing_slots_for_candidate


def proposal_from_candidate(candidate: TeamTaskTaskCandidate, *, message: IncomingMessage) -> Proposal:
    """Build the canonical internal proposal from an operating-agent candidate."""

    assigned_to = resolve_assignee(candidate.assigned_to, sender_id=message.sender_id)
    kind = proposal_kind(candidate)
    missing_slots = missing_slots_for_candidate(candidate, assigned_to=assigned_to)
    status = "approved" if kind == "reference" and not missing_slots else "draft"
    return Proposal(
        proposal_id=candidate.source_key,
        source_message_id=message.message_id,
        proposer_id=message.sender_id,
        title=candidate.title,
        raw_text=candidate.raw_text,
        kind=kind,
        status=status,
        assigned_to=assigned_to,
        task_management_area=candidate.task_management_area,
        discussion_id=candidate.discussion_id,
        message_id=candidate.message_id,
        missing_slots=missing_slots,
        due_date=candidate.due_date,
        scheduled_date=candidate.scheduled_date,
        time_window=candidate.time_window,
        source_url=candidate.source_url,
        source_export_path=candidate.source_export_path,
        created_at=message.received_at,
        updated_at=message.received_at,
        metadata={
            **candidate.metadata,
            **source_metadata(message.message_id),
            **intake_metadata(message),
            "source_text_hash": text_hash(candidate.raw_text),
            "source_item_type": candidate.item_type,
            "parser_assigned_to": candidate.assigned_to,
        },
    )


def proposal_kind(candidate: TeamTaskTaskCandidate) -> ProposalKind:
    if candidate.item_type not in PROPOSAL_KIND_VALUES:
        raise ValueError(f"unsupported item_type: {candidate.item_type!r}")
    if candidate.item_type == "reference" or candidate.disposition == "reference":
        return "reference"
    if candidate.item_type == "decision" or candidate.disposition == "decision_pending":
        return "decision"
    if candidate.item_type == "question":
        return "question"
    if candidate.item_type == "routine":
        return "routine"
    if candidate.item_type == "event":
        return "event"
    if candidate.scheduled_date is not None:
        return "event"
    return "task"


def resolve_assignee(assigned_to: str, *, sender_id: str) -> str:
    if assigned_to not in ASSIGNEE_VALUES:
        raise ValueError(f"unsupported assigned_to: {assigned_to!r}")
    if assigned_to == "me":
        return sender_id
    if assigned_to == "teammate":
        return "teammate" if sender_id != "teammate" else "me"
    return assigned_to


def text_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def source_metadata(message_id: str) -> dict[str, str]:
    if not message_id.startswith("slack/"):
        return {}
    parts = message_id.split("/")
    if len(parts) < 3:
        return {"source_provider": "slack"}
    return {
        "source_provider": "slack",
        "source_channel": parts[1],
        "source_ts": parts[2],
    }


def intake_metadata(message: IncomingMessage) -> dict[str, str]:
    metadata = {
        "source_visibility": message.visibility,
        "source_chat_id": message.chat_id,
    }
    if message.visibility == "team" and message.message_id.startswith("slack/"):
        metadata.update(
            {
                "notification_task_candidate": "true",
                "intake_policy": "user_confirmation_required",
                "intake_policy_reason": "slack_allowlisted_notification",
            }
        )
    return metadata

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .domain import Proposal
from .relations import child_proposals


@dataclass(frozen=True)
class TimelineEntry:
    event_type: str
    occurred_at: str
    proposal_id: str
    proposal_title: str
    source_message_id: str
    payload: dict[str, Any]

    def to_view(self) -> dict[str, str]:
        return {
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "proposal_id": self.proposal_id,
            "proposal_title": self.proposal_title,
            "source_message_id": self.source_message_id,
        }


def proposal_timeline(
    events: Iterable[dict[str, Any]],
    proposals: Iterable[Proposal],
    *,
    proposal_id: str,
    include_children: bool = True,
) -> tuple[TimelineEntry, ...]:
    proposal_tuple = tuple(proposals)
    by_id = {proposal.proposal_id: proposal for proposal in proposal_tuple}
    target_ids = {proposal_id}
    if include_children and proposal_id in by_id:
        target_ids.update(child.proposal_id for child in child_proposals(by_id[proposal_id], proposal_tuple))

    entries: list[TimelineEntry] = []
    for event in events:
        event_proposal_id = _event_proposal_id(event)
        if event_proposal_id not in target_ids:
            continue
        proposal = by_id.get(event_proposal_id)
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        entries.append(
            TimelineEntry(
                event_type=str(event.get("type", "")),
                occurred_at=str(event.get("occurred_at", "")),
                proposal_id=event_proposal_id,
                proposal_title=proposal.title if proposal is not None else _payload_title(payload),
                source_message_id=proposal.source_message_id if proposal is not None else _payload_source_message_id(payload),
                payload=payload,
            )
        )
    return tuple(entries)


def _event_proposal_id(event: dict[str, Any]) -> str:
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return ""
    for key in ("proposal_id", "target_proposal_id"):
        if payload.get(key):
            return str(payload[key])
    proposal = payload.get("proposal")
    if isinstance(proposal, dict) and proposal.get("proposal_id"):
        return str(proposal["proposal_id"])
    request = payload.get("request")
    if isinstance(request, dict) and request.get("proposal_id"):
        return str(request["proposal_id"])
    decision = payload.get("decision")
    if isinstance(decision, dict) and decision.get("proposal_id"):
        return str(decision["proposal_id"])
    return ""


def _payload_title(payload: dict[str, Any]) -> str:
    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        return str(proposal.get("title") or "")
    return ""


def _payload_source_message_id(payload: dict[str, Any]) -> str:
    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        return str(proposal.get("source_message_id") or "")
    return str(payload.get("source_message_id") or "")

from __future__ import annotations

from .domain import Proposal
from .relations import (
    COMPLETED_AT_KEY,
    WORKFLOW_CONTAINER_KEY,
    WORKFLOW_PARENT_ROLE,
    WORKFLOW_ROLE_KEY,
    parent_proposal_id,
)


def section_anchor(
    proposal: Proposal,
    by_id: dict[str, Proposal],
    visible_ids: set[str],
) -> Proposal:
    """Walk up the parent chain to the outermost visible/container anchor."""

    current = proposal
    anchor: Proposal | None = None
    seen: set[str] = set()
    while True:
        parent_id = parent_proposal_id(current)
        parent = by_id.get(parent_id) if parent_id else None
        if parent is None or parent.proposal_id in seen:
            break
        seen.add(parent.proposal_id)
        if parent.proposal_id in visible_ids or is_workflow_context_parent(parent):
            anchor = parent
        current = parent
    return anchor or proposal


def display_children(
    children: tuple[Proposal, ...],
    max_completed_children: int | None = None,
) -> tuple[Proposal, ...]:
    """Trim completed children beyond ``max_completed_children`` (None = keep all)."""

    if max_completed_children is None:
        return children
    completed = [child for child in children if child.status in {"done", "applied"}]
    if len(completed) <= max_completed_children:
        return children
    recent_completed_ids = {
        child.proposal_id
        for child in sorted(completed, key=completion_sort_key, reverse=True)[:max_completed_children]
    }
    return tuple(
        child
        for child in children
        if child.status not in {"done", "applied"} or child.proposal_id in recent_completed_ids
    )


def completion_sort_key(proposal: Proposal) -> tuple[str, str]:
    completed_at = proposal.metadata.get(COMPLETED_AT_KEY, "")
    updated_at = proposal.updated_at.isoformat(timespec="seconds") if proposal.updated_at else ""
    created_at = proposal.created_at.isoformat(timespec="seconds") if proposal.created_at else ""
    return (completed_at or updated_at or created_at, proposal.proposal_id)


def is_workflow_context_parent(proposal: Proposal) -> bool:
    return (
        proposal.metadata.get(WORKFLOW_CONTAINER_KEY) == "true"
        or proposal.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE
    )


__all__ = [
    "section_anchor",
    "display_children",
    "completion_sort_key",
    "is_workflow_context_parent",
]

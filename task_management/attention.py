from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Literal

from .domain import ApprovalRequest, Proposal
from .relations import blocking_dependencies


AttentionKind = Literal["progress_confirmation", "deferred_reminder", "missing_info", "approval_decision"]


@dataclass(frozen=True)
class HumanAttentionItem:
    """A human response required by the current state.

    The classifier is intentionally metadata-only: renderers can safely say
    "nothing to confirm" only when this collection is empty, without adding a
    new persistence contract.
    """

    kind: AttentionKind
    proposal: Proposal
    request: ApprovalRequest | None = None
    overdue: bool = False
    blocking_dependencies: tuple[Proposal, ...] = ()


def collect_human_attention_items(
    proposals: Iterable[Proposal],
    pending_requests: Iterable[tuple[ApprovalRequest, Proposal | None]],
    *,
    today: date,
    now: datetime | None = None,
    dependency_proposals: Iterable[Proposal] | None = None,
) -> tuple[HumanAttentionItem, ...]:
    """Collect all items that should elicit a human reply now.

    Missing-slot questions, approval decisions, reached deferred reminders, and
    due/overdue progress checks are all attention items.
    """

    items: list[HumanAttentionItem] = []
    seen: set[tuple[str, str]] = set()

    proposals_list = list(proposals)
    pending_pairs = list(pending_requests)
    dependency_list = list(dependency_proposals) if dependency_proposals is not None else proposals_list
    proposals_by_id = {proposal.proposal_id: proposal for proposal in dependency_list}
    pending_request_by_proposal = {
        proposal.proposal_id: request for request, proposal in pending_pairs if proposal is not None
    }

    for proposal in proposals_list:
        if not _requires_progress_confirmation(proposal, today=today):
            continue
        if has_deferred_missing_info(proposal):
            continue
        key = ("progress_confirmation", proposal.proposal_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            HumanAttentionItem(
                kind="progress_confirmation",
                proposal=proposal,
                overdue=proposal.due_date is not None and proposal.due_date < today,
                blocking_dependencies=blocking_dependencies(proposal, proposals_by_id),
            )
        )

    for proposal in proposals_list:
        if not _requires_deferred_reminder(proposal, today=today, now=now):
            continue
        key = ("deferred_reminder", proposal.proposal_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            HumanAttentionItem(
                kind="deferred_reminder",
                proposal=proposal,
                request=pending_request_by_proposal.get(proposal.proposal_id),
            )
        )

    for request, proposal in pending_pairs:
        if proposal is None:
            continue
        if is_deferred_missing_info_pending(proposal, today=today, now=now):
            continue
        if ("deferred_reminder", proposal.proposal_id) in seen:
            continue
        kind: AttentionKind = "missing_info" if _has_unresolved_missing_info(proposal) else "approval_decision"
        key = (kind, proposal.proposal_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(HumanAttentionItem(kind=kind, proposal=proposal, request=request))

    return tuple(sorted(items, key=_attention_sort_key))


def deferred_missing_slots(proposal: Proposal) -> tuple[str, ...]:
    raw = proposal.metadata.get("deferred_missing_slots", "")
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def has_deferred_missing_info(proposal: Proposal) -> bool:
    return bool(proposal.metadata.get("deferred_until") and _has_unresolved_missing_info(proposal))


def is_deferred_missing_info_pending(proposal: Proposal, *, today: date, now: datetime | None = None) -> bool:
    return has_deferred_missing_info(proposal) and not _deferred_is_due(proposal, today=today, now=now)


def _requires_progress_confirmation(proposal: Proposal, *, today: date) -> bool:
    return (
        proposal.status in {"approved", "applied"}
        and proposal.kind in {"task", "question", "routine"}
        and proposal.due_date is not None
        and proposal.due_date <= today
    )


def _requires_deferred_reminder(proposal: Proposal, *, today: date, now: datetime | None) -> bool:
    if not proposal.metadata.get("deferred_until"):
        return False
    if proposal.status not in {"awaiting_approval", "approved", "applied"}:
        return False
    if not _has_unresolved_missing_info(proposal):
        return False
    return _deferred_is_due(proposal, today=today, now=now)


def _has_unresolved_missing_info(proposal: Proposal) -> bool:
    return bool(proposal.missing_slots or deferred_missing_slots(proposal))


def _deferred_is_due(proposal: Proposal, *, today: date, now: datetime | None) -> bool:
    raw = proposal.metadata.get("deferred_until", "")
    if not raw:
        return True
    try:
        target = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if now is not None:
        return now >= target
    return target.date() <= today


def _attention_sort_key(item: HumanAttentionItem) -> tuple[str, str, str]:
    proposal = item.proposal
    proposal_date = proposal.scheduled_date or proposal.due_date
    kind_rank = {
        "progress_confirmation": "0",
        "deferred_reminder": "1",
        "missing_info": "2",
        "approval_decision": "3",
    }[item.kind]
    return (
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        kind_rank,
        proposal.title,
    )

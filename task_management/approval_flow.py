"""Consolidated approval state-machine helpers.

These helpers extract the three blocks that were copy-pasted across the
orchestrator's approval handlers (``handle_approval``, ``handle_feedback``,
``handle_change``, ``handle_assign`` and ``_handle_workflow_group_approval``):

(a) decision persistence  -> :func:`record_decision`
(b) HARD INVARIANT 4 promotion -> :func:`promote_if_fully_approved`
(c) approved follow-through -> :func:`approved_followthrough`

HARD INVARIANT 4: ``status == 'approved'`` IFF
``set(required_approvers) ⊆ approvals``.

The promotion helper recomputes that subset relation in exactly one place.
The deliberate per-call differences (the three different fallback statuses,
the optional status precondition, and the per-site Korean team-message text)
are passed in as explicit parameters so behavior stays byte-identical to the
inlined call sites.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable, Container

from .domain import ApprovalDecision, ApprovalRequest, Proposal
from .store import TeamTaskStore


def record_decision(
    store: TeamTaskStore,
    request: ApprovalRequest,
    *,
    approver_id: str,
    accepted: bool,
    decided_at: datetime,
) -> tuple[ApprovalRequest, ApprovalDecision]:
    """Persist an approval decision (block (a)).

    Performs the ``replace`` on the request, builds the matching
    :class:`ApprovalDecision`, saves both, and appends the
    ``approval.accepted`` / ``approval.rejected`` event. Returns the decided
    request and the decision so the caller can thread them into its result.
    """

    decision_value = "accepted" if accepted else "rejected"
    decided_request = replace(request, status=decision_value, decided_at=decided_at)
    decision = ApprovalDecision(
        request_id=request.request_id,
        proposal_id=request.proposal_id,
        approver_id=approver_id,
        decision=decision_value,
        decided_at=decided_at,
    )
    store.save_approval_request(decided_request)
    store.save_approval_decision(decision)
    store.append_event(f"approval.{decision_value}", {"decision": decision}, occurred_at=decided_at)
    return decided_request, decision


def promote_if_fully_approved(
    proposal: Proposal,
    approver_id: str,
    *,
    fallback_status: str,
    required_override: tuple[str, ...] | None = None,
    write_required: bool = False,
    decided_at: datetime,
    promote_when: Container[str] | None = None,
) -> Proposal:
    """Recompute HARD INVARIANT 4 promotion (block (b)).

    Adds ``approver_id`` to ``proposal.approvals`` (sorted, de-duplicated) and
    sets ``status = 'approved'`` IFF the required approvers are a subset of the
    resulting approvals; otherwise falls back to ``fallback_status`` (which the
    caller passes explicitly because it differs deliberately per site).

    ``required_override`` lets a site supply ``required or (approver_id,)``;
    when given together with ``write_required=True`` the resolved required set
    is written back onto the proposal (sites that use the ``or (x,)`` fallback).
    Sites that must not touch ``required_approvers`` pass ``write_required``
    False (the default).

    ``promote_when`` reproduces a status precondition: when not None, promotion
    is only eligible if ``proposal.status`` is in the container. ``None`` means
    always eligible (the behavior of every named call site).
    """

    required = proposal.required_approvers if required_override is None else required_override
    approvals = tuple(sorted(set((*proposal.approvals, approver_id))))
    eligible = promote_when is None or proposal.status in promote_when
    next_status = (
        "approved"
        if eligible and set(required).issubset(approvals)
        else fallback_status
    )
    if write_required:
        return replace(
            proposal,
            approvals=approvals,
            required_approvers=required,
            status=next_status,
            updated_at=decided_at,
        )
    return replace(
        proposal,
        approvals=approvals,
        status=next_status,
        updated_at=decided_at,
    )


def approved_followthrough(
    store: TeamTaskStore,
    proposal: Proposal,
    *,
    now: datetime,
    create_prep_subtasks: Callable[..., tuple[Proposal, ...]],
) -> tuple[Proposal, ...]:
    """Approved follow-through (block (c)).

    Appends the ``proposal.approved`` event and invokes the injected
    ``create_prep_subtasks`` callable, returning the generated prep subtasks.
    The per-site team/confirmation :class:`OutboundMessage` (with its exact
    Korean text) stays built at the call site.
    """

    store.append_event("proposal.approved", {"proposal": proposal}, occurred_at=now)
    return create_prep_subtasks(proposal, now=now)

"""Per-draft new-proposal intake subsystem.

Extracted verbatim from :class:`~task_management.orchestrator.TeamTaskOrchestrator`
to keep ``handle_message`` a thin router. :class:`ProposalIntake` owns the
non-workflow per-draft loop that turns agent ``proposal_drafts`` into saved
proposals, approval requests, and outbound messages.

Ordering is preserved byte-for-byte from the inlined orchestrator code:

    graph-normalize -> source-text dedup (#17) -> merged-duplicate short-circuit
    -> standalone-child vs initial-policy -> conflict-policy -> save -> events
    -> approved-followthrough.

HARD INVARIANT 3 (duplicate-merge: canonical kept, loser rejected with
``merged_into_proposal_id``) and HARD INVARIANT 4 are preserved by reusing the
same module-level helpers the inlined code used
(:func:`match_source_text_duplicate`/:func:`mark_source_text_duplicate`,
:func:`apply_initial_policy`/:func:`apply_conflict_policy`).

Three collaborators stay on the orchestrator and are injected as callables:
``persist_graph_normalization`` (an orchestrator method), ``prepare_standalone_child``
(the workflow-batch service method), and ``create_prep_subtasks`` (an orchestrator
method). :func:`_merge_existing_proposals` is imported from
:mod:`task_management.workflow_batch` (where the workflow extraction left it) to
avoid an import cycle with :mod:`task_management.orchestrator`.
"""

from __future__ import annotations

from typing import Callable

from .approval_policy import apply_initial_policy
from .collaboration_context import normalize_external_collaboration_candidate
from .conflict_policy import apply_conflict_policy
from .domain import (
    ApprovalRequest,
    IncomingMessage,
    OrchestrationResult,
    OutboundMessage,
    Proposal,
)
from .operating_agent import ProposalDraft
from .proposal_builder import proposal_from_candidate
from .relations import (
    CONFLICT_DETECTED_KEY,
    CONFLICT_WITH_PROPOSAL_IDS_KEY,
    MERGED_INTO_PROPOSAL_ID_KEY,
    PARENT_PROPOSAL_ID_KEY,
    is_workflow_child,
)
from .store import TeamTaskStore
from .workflow_batch import _merge_existing_proposals
from .workflow_normalizer import (
    mark_source_text_duplicate,
    match_source_text_duplicate,
    normalize_new_proposal_graph,
)


class ProposalIntake:
    """Owns the non-workflow per-draft new-proposal intake loop."""

    def __init__(
        self,
        store: TeamTaskStore,
        *,
        persist_graph_normalization: Callable[..., None],
        prepare_standalone_child: Callable[
            ...,
            tuple[Proposal, tuple[ApprovalRequest, ...], tuple[OutboundMessage, ...]],
        ],
        create_prep_subtasks: Callable[..., tuple[Proposal, ...]],
    ) -> None:
        self._store = store
        self._persist_graph_normalization = persist_graph_normalization
        self._prepare_standalone_child = prepare_standalone_child
        self._create_prep_subtasks = create_prep_subtasks

    def create_from_drafts(
        self,
        drafts: tuple[ProposalDraft, ...],
        *,
        message: IncomingMessage,
        existing_proposals: tuple[Proposal, ...],
    ) -> OrchestrationResult:
        proposals: list[Proposal] = []
        requests: list[ApprovalRequest] = []
        outbound: list[OutboundMessage] = []

        for draft in drafts:
            explicit_relation = bool(draft.metadata.get(PARENT_PROPOSAL_ID_KEY) or draft.metadata.get("parent_source_key"))
            candidate = normalize_external_collaboration_candidate(
                draft.to_candidate(),
                message=message,
                existing_proposals=existing_proposals,
            )
            proposal = proposal_from_candidate(candidate, message=message)
            normalization = normalize_new_proposal_graph(
                existing_proposals,
                (proposal,),
                normalized_at=message.received_at,
            )
            self._persist_graph_normalization(normalization.updated_existing, message=message)
            existing_proposals = _merge_existing_proposals(existing_proposals, normalization.updated_existing)
            proposal = normalization.proposals[0]
            if not _is_merged_duplicate_proposal(proposal):
                canonical = match_source_text_duplicate(proposal, existing_proposals)
                if canonical is not None:
                    proposal = mark_source_text_duplicate(
                        proposal,
                        canonical,
                        normalized_at=message.received_at,
                    )
            if _is_merged_duplicate_proposal(proposal):
                self._store.save_proposal(proposal)
                self._store.append_event(
                    "proposal.merged_duplicate",
                    {
                        "proposal": proposal,
                        "message_id": message.message_id,
                        MERGED_INTO_PROPOSAL_ID_KEY: proposal.metadata.get(MERGED_INTO_PROPOSAL_ID_KEY, ""),
                    },
                    occurred_at=message.received_at,
                )
                proposals.append(proposal)
                existing_proposals = (*existing_proposals, proposal)
                continue
            if is_workflow_child(proposal) and (
                explicit_relation or proposal.metadata.get("workflow_relation_normalized") == "true"
            ):
                proposal, new_requests, new_outbound = self._prepare_standalone_child(
                    proposal,
                    now=message.received_at,
                )
            else:
                proposal, new_requests, new_outbound = apply_initial_policy(proposal, now=message.received_at)
            proposal, conflict_requests, conflict_outbound = apply_conflict_policy(
                self._store,
                proposal,
                actor_id=message.sender_id,
                now=message.received_at,
            )
            if conflict_requests:
                new_requests = conflict_requests
                new_outbound = conflict_outbound
            self._store.save_proposal(proposal)
            self._store.append_event("proposal.created", {"proposal": proposal}, occurred_at=message.received_at)
            if proposal.metadata.get(CONFLICT_DETECTED_KEY) == "true":
                self._store.append_event(
                    "proposal.conflict_detected",
                    {
                        "proposal": proposal,
                        CONFLICT_WITH_PROPOSAL_IDS_KEY: proposal.metadata.get(CONFLICT_WITH_PROPOSAL_IDS_KEY, ""),
                    },
                    occurred_at=message.received_at,
                )
            for approver_id in proposal.approvals:
                self._store.append_event(
                    "approval.accepted",
                    {
                        "proposal_id": proposal.proposal_id,
                        "approver_id": approver_id,
                        "auto": True,
                    },
                    occurred_at=message.received_at,
                )
            proposals.append(proposal)
            existing_proposals = (*existing_proposals, proposal)
            for request in new_requests:
                self._store.save_approval_request(request)
                self._store.append_event("approval.requested", {"request": request}, occurred_at=message.received_at)
                requests.append(request)
            outbound.extend(new_outbound)
            if proposal.status == "approved":
                self._store.append_event("proposal.approved", {"proposal": proposal}, occurred_at=message.received_at)
                generated = self._create_prep_subtasks(proposal, now=message.received_at)
                proposals.extend(generated)

        return OrchestrationResult(
            proposals=tuple(proposals),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )


def _is_merged_duplicate_proposal(proposal: Proposal) -> bool:
    return proposal.status == "rejected" and bool(proposal.metadata.get(MERGED_INTO_PROPOSAL_ID_KEY))

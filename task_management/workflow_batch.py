"""Workflow-batch creation and group-approval subsystem.

Extracted verbatim from :class:`~task_management.orchestrator.TeamTaskOrchestrator`
to keep the orchestrator focused on dispatch. :class:`WorkflowBatchService` owns:

* batch intake from drafts -> :meth:`create_from_drafts`
* workflow-group preparation -> :meth:`prepare_group`
* standalone workflow-child preparation -> :meth:`prepare_standalone_child`
* shared workflow-group approval -> :meth:`handle_group_approval`

HARD INVARIANT 4 (``status == 'approved'`` IFF ``set(required_approvers) ⊆
approvals``) and the workflow-group approval semantics (single shared request;
separate-approval children; one rejection rejects the batch) are preserved by
reusing the :mod:`task_management.approval_flow` helpers
(:func:`record_decision`, :func:`promote_if_fully_approved`) exactly as the
inlined orchestrator code did.

Two collaborators stay on the orchestrator and are injected as callables:
``create_prep_subtasks`` (an orchestrator method) and ``persist_graph_normalization``
(shared with the future intake extraction; kept on the orchestrator as the
lower-risk seam).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable

from .approval_policy import (
    apply_initial_policy,
    approval_request,
    approval_message as _approval_message,
    missing_slot_question_message,
    team_message as _team_message,
    workflow_group_approval_message,
)
from .approval_flow import promote_if_fully_approved, record_decision
from .collaboration_context import normalize_external_collaboration_candidate
from .conflict_policy import cancel_prep_subtasks
from .domain import (
    ApprovalRequest,
    IncomingMessage,
    OrchestrationResult,
    OutboundMessage,
    Proposal,
)
from .operating_agent import OperatingAgentDecision
from .proposal_builder import proposal_from_candidate
from .relations import (
    PARENT_PROPOSAL_ID_KEY,
    REQUIRES_SEPARATE_APPROVAL_KEY,
    STEP_INDEX_KEY,
    WORKFLOW_GROUP_CHILD_IDS_KEY,
    WORKFLOW_GROUP_ID_KEY,
    WORKFLOW_GROUP_REQUEST_ID_KEY,
    WORKFLOW_PARENT_ROLE,
    WORKFLOW_ROLE_KEY,
    WORKFLOW_SEPARATE_CHILD_IDS_KEY,
    child_proposals,
    is_workflow_child,
    is_workflow_parent,
    parent_proposal_id,
    requires_separate_approval,
    resolve_same_batch_relation_metadata,
    validate_workflow_relations,
    workflow_group_id,
)
from .store import TeamTaskStore
from .workflow_normalizer import normalize_new_proposal_graph


class WorkflowBatchService:
    """Owns workflow-batch creation and the shared group-approval flow."""

    def __init__(
        self,
        store: TeamTaskStore,
        *,
        create_prep_subtasks: Callable[..., tuple[Proposal, ...]],
        persist_graph_normalization: Callable[..., None],
    ) -> None:
        self._store = store
        self._create_prep_subtasks = create_prep_subtasks
        self._persist_graph_normalization = persist_graph_normalization

    def create_from_drafts(
        self,
        decision: OperatingAgentDecision,
        *,
        message: IncomingMessage,
        existing_proposals: tuple[Proposal, ...],
    ) -> OrchestrationResult:
        base_proposals: list[Proposal] = []
        for draft in decision.proposal_drafts:
            candidate = normalize_external_collaboration_candidate(
                draft.to_candidate(),
                message=message,
                existing_proposals=existing_proposals,
            )
            base_proposals.append(proposal_from_candidate(candidate, message=message))

        resolved = resolve_same_batch_relation_metadata(tuple(base_proposals))
        normalization = normalize_new_proposal_graph(
            existing_proposals,
            resolved,
            normalized_at=message.received_at,
        )
        self._persist_graph_normalization(normalization.updated_existing, message=message)
        existing_proposals = _merge_existing_proposals(existing_proposals, normalization.updated_existing)
        resolved = normalization.proposals
        validation_errors = validate_workflow_relations(resolved, existing_proposals=existing_proposals)
        if validation_errors:
            self._store.append_event(
                "workflow.batch.rejected",
                {
                    "message_id": message.message_id,
                    "errors": [
                        {"code": error.code, "proposal_id": error.proposal_id, "detail": error.detail}
                        for error in validation_errors
                    ],
                },
                occurred_at=message.received_at,
            )
            return OrchestrationResult(
                outbound_messages=(
                    OutboundMessage(
                        surface="personal_chat",
                        recipient_id=message.sender_id,
                        message_type="workflow_batch_rejected",
                        text="연속 작업 구조를 확정하기 전에 관계 정보를 다시 확인해야 합니다.",
                        card={
                            "message_id": message.message_id,
                            "error_codes": ",".join(error.code for error in validation_errors),
                            "proposal_ids": ",".join(error.proposal_id for error in validation_errors),
                        },
                    ),
                )
            )

        all_for_projection = (*existing_proposals, *resolved)
        parent_ids = {
            parent_proposal_id(proposal)
            for proposal in resolved
            if parent_proposal_id(proposal)
        }
        resolved_ids = {proposal.proposal_id for proposal in resolved}
        saved: list[Proposal] = []
        requests: list[ApprovalRequest] = []
        outbound: list[OutboundMessage] = []
        handled_ids: set[str] = set()

        for parent in resolved:
            if parent.proposal_id in handled_ids:
                continue
            if parent.proposal_id not in parent_ids and not is_workflow_parent(parent, all_for_projection):
                continue
            children = tuple(
                child
                for child in child_proposals(parent, all_for_projection)
                if child.proposal_id in resolved_ids and child.proposal_id not in handled_ids
            )
            if not children:
                continue
            group_result = self.prepare_group(parent, children, now=message.received_at)
            saved.extend(group_result.proposals)
            requests.extend(group_result.approval_requests)
            outbound.extend(group_result.outbound_messages)
            handled_ids.update(proposal.proposal_id for proposal in group_result.proposals)

        for proposal in resolved:
            if proposal.proposal_id in handled_ids:
                continue
            if is_workflow_child(proposal):
                proposal, new_requests, new_outbound = self.prepare_standalone_child(
                    proposal,
                    now=message.received_at,
                )
            else:
                proposal, new_requests, new_outbound = apply_initial_policy(proposal, now=message.received_at)
            saved.append(proposal)
            requests.extend(new_requests)
            outbound.extend(new_outbound)
            handled_ids.add(proposal.proposal_id)

        for proposal in saved:
            self._store.save_proposal(proposal)
        for proposal in saved:
            self._store.append_event("proposal.created", {"proposal": proposal}, occurred_at=message.received_at)
        if any(is_workflow_parent(proposal, saved) or is_workflow_child(proposal) for proposal in saved):
            self._store.append_event(
                "workflow.split.created",
                {
                    "message_id": message.message_id,
                    "proposal_ids": [proposal.proposal_id for proposal in saved],
                    "source": decision.source,
                    "confidence": decision.confidence,
                },
                occurred_at=message.received_at,
            )
        for request in requests:
            self._store.save_approval_request(request)
            event_type = (
                "workflow.group_approval.requested"
                if any(
                    proposal.proposal_id == request.proposal_id
                    and proposal.metadata.get(WORKFLOW_GROUP_REQUEST_ID_KEY) == request.request_id
                    for proposal in saved
                )
                else "approval.requested"
            )
            self._store.append_event(event_type, {"request": request}, occurred_at=message.received_at)
        return OrchestrationResult(
            proposals=tuple(saved),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )

    def prepare_group(
        self,
        parent: Proposal,
        children: tuple[Proposal, ...],
        *,
        now: datetime,
    ) -> OrchestrationResult:
        approver = parent.proposer_id or "me"
        group_id = workflow_group_id(parent)
        normal_children = tuple(child for child in children if not _child_needs_separate_workflow_approval(child))
        separate_children = tuple(child for child in children if _child_needs_separate_workflow_approval(child))
        group_request = approval_request(parent.proposal_id, approver, now=now)

        parent_metadata = {
            **parent.metadata,
            WORKFLOW_ROLE_KEY: WORKFLOW_PARENT_ROLE,
            WORKFLOW_GROUP_ID_KEY: group_id,
            WORKFLOW_GROUP_REQUEST_ID_KEY: group_request.request_id,
            WORKFLOW_GROUP_CHILD_IDS_KEY: ",".join(child.proposal_id for child in normal_children),
            WORKFLOW_SEPARATE_CHILD_IDS_KEY: ",".join(child.proposal_id for child in separate_children),
        }
        grouped_parent = replace(
            parent,
            status="awaiting_approval",
            required_approvers=(approver,),
            approvals=(),
            missing_slots=(),
            updated_at=now,
            metadata=parent_metadata,
        )
        prepared: list[Proposal] = [grouped_parent]
        requests: list[ApprovalRequest] = [group_request]
        outbound: list[OutboundMessage] = [
            workflow_group_approval_message(
                grouped_parent,
                group_request,
                children=normal_children,
                separate_children=separate_children,
            )
        ]

        for child in normal_children:
            prepared.append(
                replace(
                    child,
                    status="awaiting_approval",
                    required_approvers=(approver,),
                    approvals=(),
                    updated_at=now,
                    metadata={
                        **child.metadata,
                        WORKFLOW_GROUP_ID_KEY: group_id,
                        WORKFLOW_GROUP_REQUEST_ID_KEY: group_request.request_id,
                    },
                )
            )

        for child in separate_children:
            request = approval_request(child.proposal_id, approver, now=now)
            risky = replace(
                child,
                kind="question" if child.missing_slots else child.kind,
                status="awaiting_approval",
                required_approvers=(approver,),
                approvals=(),
                updated_at=now,
                metadata={
                    **child.metadata,
                    WORKFLOW_GROUP_ID_KEY: group_id,
                    "workflow_separate_approval": "true",
                    REQUIRES_SEPARATE_APPROVAL_KEY: "true",
                },
            )
            prepared.append(risky)
            requests.append(request)
            if risky.missing_slots:
                outbound.append(missing_slot_question_message(risky, request))
            else:
                outbound.append(
                    _approval_message(
                        risky,
                        request,
                        text=f"별도 확인이 필요한 단계입니다: {risky.title}",
                    )
                )

        return OrchestrationResult(
            proposals=tuple(prepared),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )

    def prepare_standalone_child(
        self,
        proposal: Proposal,
        *,
        now: datetime,
    ) -> tuple[Proposal, tuple[ApprovalRequest, ...], tuple[OutboundMessage, ...]]:
        """Keep existing-parent workflow children out of personal auto-approval."""

        approver = proposal.proposer_id or "me"
        request = approval_request(proposal.proposal_id, approver, now=now)
        parent_id = parent_proposal_id(proposal)
        metadata = dict(proposal.metadata)
        if parent_id:
            metadata.setdefault(WORKFLOW_GROUP_ID_KEY, f"workflow-group/{parent_id}")
        awaiting = replace(
            proposal,
            kind="question" if proposal.missing_slots else proposal.kind,
            status="awaiting_approval",
            required_approvers=(approver,),
            approvals=(),
            updated_at=now,
            metadata=metadata,
        )
        if awaiting.missing_slots:
            message = missing_slot_question_message(awaiting, request)
        else:
            message = _approval_message(
                awaiting,
                request,
                text=f"워크플로 단계 확인이 필요합니다: {awaiting.title}",
            )
        return awaiting, (request,), (message,)

    def handle_group_approval(
        self,
        *,
        request: ApprovalRequest,
        proposal: Proposal,
        approver_id: str,
        accepted: bool,
        decided_at: datetime,
    ) -> OrchestrationResult:
        decided_request, _decision = record_decision(
            self._store,
            request,
            approver_id=approver_id,
            accepted=accepted,
            decided_at=decided_at,
        )

        child_ids = _split_csv(proposal.metadata.get(WORKFLOW_GROUP_CHILD_IDS_KEY, ""))
        child_proposals_to_update = [
            child
            for child_id in child_ids
            for child in (self._store.get_proposal(child_id),)
            if child is not None
        ]
        updated: list[Proposal] = []
        if accepted:
            required = proposal.required_approvers or (approver_id,)
            updated_parent = promote_if_fully_approved(
                proposal,
                approver_id,
                fallback_status="awaiting_approval",
                required_override=required,
                write_required=True,
                decided_at=decided_at,
            )
            self._store.save_proposal(updated_parent)
            updated.append(updated_parent)
            if updated_parent.status == "approved":
                self._store.append_event("proposal.approved", {"proposal": updated_parent}, occurred_at=decided_at)
            for child in child_proposals_to_update:
                updated_child = promote_if_fully_approved(
                    child,
                    approver_id,
                    fallback_status="awaiting_approval",
                    required_override=child.required_approvers or required,
                    write_required=True,
                    decided_at=decided_at,
                )
                self._store.save_proposal(updated_child)
                updated.append(updated_child)
                if updated_child.status == "approved":
                    self._store.append_event(
                        "proposal.approved",
                        {"proposal": updated_child, "workflow_group_id": workflow_group_id(proposal)},
                        occurred_at=decided_at,
                    )
            self._store.append_event(
                "workflow.group_approval.accepted",
                {
                    "request_id": request.request_id,
                    "parent_proposal_id": proposal.proposal_id,
                    "child_proposal_ids": child_ids,
                },
                occurred_at=decided_at,
            )
            return OrchestrationResult(
                proposals=tuple(updated),
                approval_requests=(decided_request,),
                outbound_messages=(
                    _team_message(
                        updated[0],
                        message_type="workflow_group_approved",
                        text=f"'{updated[0].title}' 묶음을 등록했습니다.",
                    ),
                ),
            )

        updated_parent = replace(proposal, status="rejected", updated_at=decided_at)
        self._store.save_proposal(updated_parent)
        self._store.append_event("proposal.rejected", {"proposal": updated_parent}, occurred_at=decided_at)
        updated.append(updated_parent)
        cascade_targets = [updated_parent.proposal_id]
        for child in child_proposals_to_update:
            updated_child = replace(child, status="rejected", updated_at=decided_at)
            self._store.save_proposal(updated_child)
            self._store.append_event(
                "proposal.rejected",
                {"proposal": updated_child, "workflow_group_id": workflow_group_id(proposal)},
                occurred_at=decided_at,
            )
            updated.append(updated_child)
            cascade_targets.append(updated_child.proposal_id)
        for target_id in cascade_targets:
            updated.extend(
                cancel_prep_subtasks(
                    self._store,
                    target_id,
                    now=decided_at,
                    reason="workflow_group_rejected",
                )
            )
        self._store.append_event(
            "workflow.group_approval.rejected",
            {
                "request_id": request.request_id,
                "parent_proposal_id": proposal.proposal_id,
                "child_proposal_ids": child_ids,
            },
            occurred_at=decided_at,
        )
        return OrchestrationResult(
            proposals=tuple(updated),
            approval_requests=(decided_request,),
            outbound_messages=(
                _team_message(
                    updated_parent,
                    message_type="workflow_group_rejected",
                    text=f"연속 작업 묶음을 보류했습니다: {updated_parent.title}",
                ),
            ),
        )


def decision_contains_workflow_batch(decision: OperatingAgentDecision) -> bool:
    if not decision.proposal_drafts:
        return False
    if any(
        draft.metadata.get(PARENT_PROPOSAL_ID_KEY) or draft.metadata.get("parent_source_key")
        for draft in decision.proposal_drafts
    ):
        return True
    if len(decision.proposal_drafts) < 2:
        return False
    parent_keys = {
        draft.source_key
        for draft in decision.proposal_drafts
        if draft.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE
    }
    for draft in decision.proposal_drafts:
        metadata = draft.metadata
        if metadata.get(PARENT_PROPOSAL_ID_KEY) or metadata.get("parent_source_key"):
            return True
        if metadata.get("workflow_id") and parent_keys:
            return True
        if metadata.get(WORKFLOW_GROUP_ID_KEY) or metadata.get(STEP_INDEX_KEY):
            return True
    return False


def _merge_existing_proposals(
    existing: tuple[Proposal, ...],
    updates: tuple[Proposal, ...],
) -> tuple[Proposal, ...]:
    if not updates:
        return existing
    by_id = {proposal.proposal_id: proposal for proposal in existing}
    for update in updates:
        by_id[update.proposal_id] = update
    return tuple(by_id.values())


def _child_needs_separate_workflow_approval(proposal: Proposal) -> bool:
    return bool(proposal.missing_slots) or requires_separate_approval(proposal)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())

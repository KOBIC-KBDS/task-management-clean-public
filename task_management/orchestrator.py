from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import hashlib
import os
import re
from typing import Iterable

from .commands import InstanceProbeCommand, parse_chat_command, parse_instance_probe
from .conflict_policy import apply_conflict_policy
from .deferred_policy import csv_dedupe, default_deferred_until
from .discussion_adapter import parse_temporal_update
from .domain import (
    Actor,
    ApprovalDecision,
    ApprovalRequest,
    IncomingMessage,
    OrchestrationResult,
    OutboundMessage,
    Proposal,
    ProposalKind,
)
from .approval_policy import (
    apply_assignment_policy,
    apply_initial_policy,
    approval_request,
    approval_message as _approval_message,
    missing_slot_question_message,
    team_message as _team_message,
    workflow_group_approval_message,
)
from .collaboration_context import normalize_external_collaboration_candidate
from .operating_agent import (
    TeamTaskOperatingAgent,
    OperatingAgentDecision,
    ProposalPatch,
    RuleBasedTeamTaskOperatingAgent,
    decision_to_payload,
)
from .proposal_builder import proposal_from_candidate, text_hash
from .pending_info import missing_info_followup_message
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
from .slot_validator import missing_slots_for_proposal
from .sort_keys import time_sort_minutes
from .store import TeamTaskStore
from .task_reconciler import apply_state_linked_update
from .workflow_normalizer import normalize_new_proposal_graph


DEFAULT_ACTORS = (
    Actor(actor_id="me", display_name="나", aliases=("나", "엄마", "me")),
    Actor(actor_id="teammate", display_name="팀원", aliases=("팀원", "팀원", "teammate")),
)
MIN_SEMANTIC_TARGET_CONFIDENCE = 0.65
_SEMANTIC_CORRECTION_KEYS = (
    "title",
    "corrected_title",
    "due_date",
    "scheduled_date",
    "date_window_start",
    "time_window",
    "participants",
    "external_owner",
    "external_participants",
    "participant_label",
    "attendees",
    "location",
    "location_optional",
    "materials",
    "needs_prep",
    "needs_exact_time",
)


class TeamTaskOrchestrator:
    def __init__(
        self,
        store: TeamTaskStore,
        *,
        actors: Iterable[Actor] = DEFAULT_ACTORS,
        operating_agent: TeamTaskOperatingAgent | None = None,
    ) -> None:
        self.store = store
        self.actors = {actor.actor_id: actor for actor in actors}
        self.operating_agent = operating_agent or RuleBasedTeamTaskOperatingAgent()

    def handle_message(self, message: IncomingMessage) -> OrchestrationResult:
        if self.store.has_message(message.message_id):
            return OrchestrationResult(ignored_duplicate=True)

        self.store.record_message(message)
        self.store.append_event("message.received", {"message": message}, occurred_at=message.received_at)

        instance_probe = parse_instance_probe(message.text)
        if instance_probe is not None:
            return self._handle_instance_probe(instance_probe, message)

        command = parse_chat_command(message.text)
        if command is not None:
            return self._handle_command_message(command, message)

        existing_proposals = self.store.list_proposals()
        pending_approval_requests = self.store.list_approval_requests(
            approver_id=message.sender_id,
            status="pending",
        )
        message = replace(
            message,
            recent_conversation=_recent_conversation_from_events(
                self.store.read_events(),
                chat_id=message.chat_id,
                sender_id=message.sender_id,
            ),
        )
        decision = self.operating_agent.decide(
            message,
            pending_approval_requests=pending_approval_requests,
            pending_proposals=existing_proposals,
        )
        self.store.append_event(
            "agent.decision.created",
            {"decision": decision_to_payload(decision)},
            occurred_at=message.received_at,
        )
        if "fallback" in decision.source:
            self.store.append_event(
                "agent.fallback.used",
                {
                    "source": decision.source,
                    "message_id": message.message_id,
                    "rationale": decision.rationale,
                },
                occurred_at=message.received_at,
            )
        proposals: list[Proposal] = []
        requests: list[ApprovalRequest] = []
        outbound: list[OutboundMessage] = []

        if decision.action in {"apply_feedback", "create_proposals"} and decision.proposal_patches:
            feedback_result = self._handle_agent_feedback_patches(
                decision.proposal_patches,
                actor_id=message.sender_id,
                changed_at=message.received_at,
            )
            proposals.extend(feedback_result.proposals)
            requests.extend(feedback_result.approval_requests)
            outbound.extend(feedback_result.outbound_messages)
            if decision.action == "apply_feedback" and not decision.proposal_drafts:
                return feedback_result
        if decision.clarification_questions:
            return OrchestrationResult(
                proposals=tuple(proposals),
                approval_requests=tuple(requests),
                outbound_messages=tuple(outbound)
                + tuple(
                    _agent_clarification_message(
                        recipient_id=question.recipient_id,
                        prompt=question.prompt,
                        proposal_id=question.proposal_id,
                        missing_slots=question.missing_slots,
                    )
                    for question in decision.clarification_questions
                )
            )
        if decision.action == "no_action":
            state_updates = self._state_linked_fallback_after_agent(message, decision)
            if state_updates:
                return OrchestrationResult(
                    proposals=state_updates,
                    outbound_messages=tuple(
                        _state_update_message(proposal, actor_id=message.sender_id)
                        for proposal in state_updates
                    ),
                )
            return OrchestrationResult(
                proposals=tuple(proposals),
                approval_requests=tuple(requests),
                outbound_messages=tuple(outbound),
            )

        existing_proposals = self.store.list_proposals()
        if decision.proposal_drafts and _decision_contains_workflow_batch(decision):
            workflow_result = self._handle_workflow_proposal_drafts(
                decision,
                message=message,
                existing_proposals=existing_proposals,
            )
            proposals.extend(workflow_result.proposals)
            requests.extend(workflow_result.approval_requests)
            outbound.extend(workflow_result.outbound_messages)
            return OrchestrationResult(
                proposals=tuple(proposals),
                approval_requests=tuple(requests),
                outbound_messages=tuple(outbound),
            )

        for draft in decision.proposal_drafts:
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
            if is_workflow_child(proposal) and (
                explicit_relation or proposal.metadata.get("workflow_relation_normalized") == "true"
            ):
                proposal, new_requests, new_outbound = self._prepare_standalone_workflow_child(
                    proposal,
                    now=message.received_at,
                )
            else:
                proposal, new_requests, new_outbound = apply_initial_policy(proposal, now=message.received_at)
            proposal, conflict_requests, conflict_outbound = apply_conflict_policy(
                self.store,
                proposal,
                actor_id=message.sender_id,
                now=message.received_at,
            )
            if conflict_requests:
                new_requests = conflict_requests
                new_outbound = conflict_outbound
            self.store.save_proposal(proposal)
            self.store.append_event("proposal.created", {"proposal": proposal}, occurred_at=message.received_at)
            if proposal.metadata.get("conflict_detected") == "true":
                self.store.append_event(
                    "proposal.conflict_detected",
                    {
                        "proposal": proposal,
                        "conflict_with_proposal_ids": proposal.metadata.get("conflict_with_proposal_ids", ""),
                    },
                    occurred_at=message.received_at,
                )
            for approver_id in proposal.approvals:
                self.store.append_event(
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
                self.store.save_approval_request(request)
                self.store.append_event("approval.requested", {"request": request}, occurred_at=message.received_at)
                requests.append(request)
            outbound.extend(new_outbound)
            if proposal.status == "approved":
                self.store.append_event("proposal.approved", {"proposal": proposal}, occurred_at=message.received_at)
                generated = self._create_prep_subtasks(proposal, now=message.received_at)
                proposals.extend(generated)

        return OrchestrationResult(
            proposals=tuple(proposals),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )

    def _handle_workflow_proposal_drafts(
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
            self.store.append_event(
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
            if parent.proposal_id not in parent_ids and not is_workflow_parent(parent, all_for_projection):
                continue
            children = tuple(child for child in child_proposals(parent, all_for_projection) if child.proposal_id in resolved_ids)
            if not children:
                continue
            group_result = self._prepare_workflow_group(parent, children, now=message.received_at)
            saved.extend(group_result.proposals)
            requests.extend(group_result.approval_requests)
            outbound.extend(group_result.outbound_messages)
            handled_ids.update(proposal.proposal_id for proposal in group_result.proposals)

        for proposal in resolved:
            if proposal.proposal_id in handled_ids:
                continue
            if is_workflow_child(proposal):
                proposal, new_requests, new_outbound = self._prepare_standalone_workflow_child(
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
            self.store.save_proposal(proposal)
        for proposal in saved:
            self.store.append_event("proposal.created", {"proposal": proposal}, occurred_at=message.received_at)
        if any(is_workflow_parent(proposal, saved) or is_workflow_child(proposal) for proposal in saved):
            self.store.append_event(
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
            self.store.save_approval_request(request)
            event_type = (
                "workflow.group_approval.requested"
                if any(
                    proposal.proposal_id == request.proposal_id
                    and proposal.metadata.get(WORKFLOW_GROUP_REQUEST_ID_KEY) == request.request_id
                    for proposal in saved
                )
                else "approval.requested"
            )
            self.store.append_event(event_type, {"request": request}, occurred_at=message.received_at)
        return OrchestrationResult(
            proposals=tuple(saved),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )

    def _persist_graph_normalization(
        self,
        updated_existing: tuple[Proposal, ...],
        *,
        message: IncomingMessage,
    ) -> None:
        for updated in updated_existing:
            self.store.save_proposal(updated)
            self.store.append_event(
                "workflow.graph_normalized",
                {
                    "proposal": updated,
                    "message_id": message.message_id,
                    "reason": updated.metadata.get("workflow_rename_reason", "relation_normalized"),
                },
                occurred_at=message.received_at,
            )

    def _prepare_workflow_group(
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

    def _prepare_standalone_workflow_child(
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

    def _state_linked_fallback_after_agent(
        self,
        message: IncomingMessage,
        decision: OperatingAgentDecision,
    ) -> tuple[Proposal, ...]:
        if not _should_run_state_linked_fallback(message, decision):
            return ()
        return apply_state_linked_update(self.store, message, updated_at=message.received_at)

    def _handle_instance_probe(self, probe: InstanceProbeCommand, message: IncomingMessage) -> OrchestrationResult:
        instance_id = os.environ.get("TASK_MANAGEMENT_INSTANCE_ID", "")
        if instance_id != probe.target_instance_id:
            self.store.append_event(
                "instance_probe.ignored",
                {
                    "target_instance_id": probe.target_instance_id,
                    "configured_instance_id": instance_id,
                    "nonce": probe.nonce,
                },
                occurred_at=message.received_at,
            )
            return OrchestrationResult()

        self.store.append_event(
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

    def _handle_command_message(self, command, message: IncomingMessage) -> OrchestrationResult:
        if command.action in {"accept", "reject"}:
            return self.handle_approval(
                request_id=command.target_id,
                approver_id=message.sender_id,
                accepted=command.action == "accept",
                decided_at=message.received_at,
            )
        if command.action == "change":
            return self.handle_change(
                target_id=command.target_id,
                actor_id=message.sender_id,
                body=command.body,
                changed_at=message.received_at,
            )
        if command.action == "assign":
            return self.handle_assign(
                target_id=command.target_id,
                actor_id=message.sender_id,
                body=command.body,
                assigned_at=message.received_at,
            )
        if command.action == "complete":
            return self.handle_complete(
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

    def _handle_agent_feedback_patches(
        self,
        patches: tuple[ProposalPatch, ...],
        *,
        actor_id: str,
        changed_at: datetime,
    ) -> OrchestrationResult:
        proposals: list[Proposal] = []
        requests: list[ApprovalRequest] = []
        outbound: list[OutboundMessage] = []
        for patch in patches:
            rejection = self._semantic_patch_rejection_reason(patch, actor_id=actor_id)
            if rejection:
                self.store.append_event(
                    "agent.patch.rejected",
                    {
                        "patch": patch.to_payload(),
                        "reason": rejection,
                    },
                    occurred_at=changed_at,
                )
                outbound.append(_patch_rejection_message(patch, actor_id=actor_id, reason=rejection))
                continue

            self.store.append_event(
                "agent.patch.accepted",
                {
                    "patch": patch.to_payload(),
                    "target_confidence": patch.target_confidence,
                    "evidence_text": patch.evidence_text,
                },
                occurred_at=changed_at,
            )
            result = self.handle_feedback(
                request_id=patch.request_id,
                actor_id=actor_id,
                body=patch.body,
                changed_at=changed_at,
                temporal_update=dict(patch.temporal_update),
                expected_proposal_id=patch.proposal_id,
            ) if patch.request_id else self._handle_direct_semantic_patch(
                patch,
                actor_id=actor_id,
                changed_at=changed_at,
            )
            proposals.extend(result.proposals)
            requests.extend(result.approval_requests)
            outbound.extend(result.outbound_messages)
        return OrchestrationResult(
            proposals=tuple(proposals),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )

    def _semantic_patch_rejection_reason(self, patch: ProposalPatch, *, actor_id: str) -> str:
        if patch.actor_id and patch.actor_id != actor_id:
            return "actor_mismatch"
        if patch.needs_clarification:
            return "agent_requested_clarification"
        if patch.target_confidence < MIN_SEMANTIC_TARGET_CONFIDENCE:
            return "low_target_confidence"
        semantic_shape_rejection = _semantic_update_shape_rejection(patch.temporal_update)
        if semantic_shape_rejection:
            return semantic_shape_rejection
        if not patch.request_id:
            if not patch.proposal_id:
                return "missing_proposal"
            proposal = self.store.get_proposal(patch.proposal_id)
            if proposal is None:
                return "missing_proposal"
            if actor_id not in {proposal.proposer_id, proposal.assigned_to, *proposal.required_approvers, *proposal.approvals}:
                return "actor_not_authorized_for_direct_patch"
            if not _meaningful_semantic_update(patch.temporal_update):
                return "empty_semantic_update"
            if _requires_actionable_direct_patch(patch.temporal_update) and (
                proposal.status not in {"approved", "applied"} or proposal.missing_slots
            ):
                return "target_not_actionable_for_direct_patch"
            return ""
        request = self.store.get_approval_request(patch.request_id)
        if request is None:
            return "missing_request"
        if request.status != "pending":
            return "request_not_pending"
        if request.approver_id != actor_id:
            return "approver_mismatch"
        if patch.proposal_id and request.proposal_id != patch.proposal_id:
            return "proposal_request_mismatch"
        return ""

    def _handle_direct_semantic_patch(
        self,
        patch: ProposalPatch,
        *,
        actor_id: str,
        changed_at: datetime,
    ) -> OrchestrationResult:
        proposal = self.store.get_proposal(patch.proposal_id)
        if proposal is None:
            return OrchestrationResult()

        if _is_scoped_progress_completion(patch.temporal_update):
            scoped_update = _progress_update_from_scoped_completion(patch.temporal_update)
            return self._handle_direct_progress_patch(
                replace(patch, temporal_update=scoped_update),
                actor_id=actor_id,
                changed_at=changed_at,
                proposal=proposal,
            )
        if _is_confirmation_update(patch.temporal_update):
            temporal_update = dict(patch.temporal_update)
            temporal_update.pop("status", None)
            temporal_update["semantic_update_type"] = "confirmation"
            patch = replace(patch, temporal_update=temporal_update)
        if patch.temporal_update.get("status") == "done":
            return self._handle_direct_completion_patch(patch, actor_id=actor_id, changed_at=changed_at, proposal=proposal)
        if patch.temporal_update.get("progress_status"):
            return self._handle_direct_progress_patch(patch, actor_id=actor_id, changed_at=changed_at, proposal=proposal)

        changed = _apply_temporal_change(
            proposal,
            dict(patch.temporal_update),
            actor_id=actor_id,
            changed_at=changed_at,
        )
        linked_type = (
            "semantic_deferral"
            if patch.temporal_update.get("semantic_update_type") == "deferral"
            else "semantic_confirmation"
            if patch.temporal_update.get("semantic_update_type") == "confirmation"
            else "semantic_correction"
            if patch.temporal_update.get("semantic_update_type") == "correction"
            else "semantic_direct_update"
        )
        metadata = {
            **changed.metadata,
            "last_semantic_patch_actor_id": actor_id,
            "last_semantic_patch_at": changed_at.isoformat(timespec="seconds"),
            "last_semantic_patch_confidence": f"{patch.target_confidence:.2f}",
            "last_semantic_patch_evidence": patch.evidence_text,
            "last_state_linked_update_type": linked_type,
        }
        missing_slots = missing_slots_for_proposal(replace(changed, metadata=metadata))
        status = changed.status
        required = changed.required_approvers or (actor_id,)
        approvals = changed.approvals or ((actor_id,) if changed.assigned_to == actor_id else ())
        if missing_slots:
            status = "awaiting_approval"
            approvals = tuple(item for item in approvals if item in required)
        elif status in {"draft", "awaiting_approval"} and set(required).issubset(set((*approvals, actor_id))):
            status = "approved"
            approvals = tuple(sorted(set((*approvals, actor_id))))
        updated = replace(
            changed,
            status=status,
            required_approvers=required,
            approvals=approvals,
            missing_slots=missing_slots,
            metadata=metadata,
            updated_at=changed_at,
        )
        self.store.save_proposal(updated)
        self.store.append_event(
            "proposal.changed",
            {
                "proposal": updated,
                "change_body": patch.body,
                "actor_id": actor_id,
                "semantic_patch": patch.to_payload(),
            },
            occurred_at=changed_at,
        )
        if not missing_slots and updated.status == "approved":
            self.store.append_event("proposal.approved", {"proposal": updated, "semantic_patch": True}, occurred_at=changed_at)
        if missing_slots:
            request, created = self._ensure_missing_slot_request(updated, actor_id=actor_id, now=changed_at)
            if created:
                self.store.append_event(
                    "approval.requested",
                    {"request": request, "source": "semantic_patch_missing_slots"},
                    occurred_at=changed_at,
                )
            message = (
                missing_slot_question_message(updated, request)
                if created
                else missing_info_followup_message(updated, request, now=changed_at)
            )
            return OrchestrationResult(
                proposals=(updated,),
                approval_requests=(request,),
                outbound_messages=(message,),
            )
        return OrchestrationResult(
            proposals=(updated,),
            outbound_messages=(
                _semantic_direct_update_message(updated, actor_id=actor_id),
            ),
        )

    def _handle_direct_completion_patch(
        self,
        patch: ProposalPatch,
        *,
        actor_id: str,
        changed_at: datetime,
        proposal: Proposal,
    ) -> OrchestrationResult:
        base = _apply_temporal_change(
            proposal,
            dict(patch.temporal_update),
            actor_id=actor_id,
            changed_at=changed_at,
        )
        metadata = {
            **base.metadata,
            "completed_by": actor_id,
            "completed_at": changed_at.isoformat(timespec="seconds"),
            "completion_source": "semantic_patch",
            "last_semantic_patch_actor_id": actor_id,
            "last_semantic_patch_at": changed_at.isoformat(timespec="seconds"),
            "last_semantic_patch_confidence": f"{patch.target_confidence:.2f}",
            "last_semantic_patch_evidence": patch.evidence_text,
            "last_state_linked_update_type": "semantic_completion",
        }
        updated = replace(base, status="done", metadata=metadata, updated_at=changed_at)
        self.store.save_proposal(updated)
        self.store.append_event(
            "proposal.completed",
            {"proposal": updated, "actor_id": actor_id, "semantic_patch": patch.to_payload()},
            occurred_at=changed_at,
        )
        related = self._complete_related_scheduled_commitments(
            updated,
            patch,
            actor_id=actor_id,
            changed_at=changed_at,
        )
        return OrchestrationResult(
            proposals=(updated, *related),
            outbound_messages=(_semantic_direct_update_message(updated, actor_id=actor_id),),
        )

    def _complete_related_scheduled_commitments(
        self,
        completed: Proposal,
        patch: ProposalPatch,
        *,
        actor_id: str,
        changed_at: datetime,
    ) -> tuple[Proposal, ...]:
        if completed.kind != "event" or completed.scheduled_date is None:
            return ()
        related: list[Proposal] = []
        for candidate in self.store.list_proposals():
            if candidate.proposal_id == completed.proposal_id:
                continue
            if not _same_scheduled_commitment_slot(candidate, completed):
                continue
            if not _has_related_completion_signal(candidate, completed, patch):
                continue
            metadata = {
                **candidate.metadata,
                "completed_by": actor_id,
                "completed_at": changed_at.isoformat(timespec="seconds"),
                "completion_source": "semantic_linked_completion",
                "linked_completion_source_proposal_id": completed.proposal_id,
                "last_semantic_patch_actor_id": actor_id,
                "last_semantic_patch_at": changed_at.isoformat(timespec="seconds"),
                "last_semantic_patch_confidence": f"{patch.target_confidence:.2f}",
                "last_semantic_patch_evidence": patch.evidence_text,
                "last_state_linked_update_type": "semantic_linked_completion",
            }
            updated = replace(candidate, status="done", metadata=metadata, updated_at=changed_at)
            self.store.save_proposal(updated)
            self.store.append_event(
                "proposal.completed",
                {
                    "proposal": updated,
                    "actor_id": actor_id,
                    "semantic_patch": patch.to_payload(),
                    "linked_completion_source_proposal_id": completed.proposal_id,
                },
                occurred_at=changed_at,
            )
            related.append(updated)
        return tuple(related)

    def _handle_direct_progress_patch(
        self,
        patch: ProposalPatch,
        *,
        actor_id: str,
        changed_at: datetime,
        proposal: Proposal,
    ) -> OrchestrationResult:
        base = _apply_temporal_change(
            proposal,
            dict(patch.temporal_update),
            actor_id=actor_id,
            changed_at=changed_at,
        )
        if _has_temporal_or_slot_change(patch.temporal_update):
            base = replace(base, missing_slots=missing_slots_for_proposal(base))
        else:
            base = replace(base, missing_slots=proposal.missing_slots)
        linked_type = (
            "semantic_correction"
            if patch.temporal_update.get("semantic_update_type") == "correction"
            else "semantic_deferral"
            if patch.temporal_update.get("semantic_update_type") == "deferral"
            else "semantic_progress"
        )
        metadata = {
            **base.metadata,
            "progress_status": patch.temporal_update.get("progress_status", "partial"),
            "progress_note": patch.body.strip(),
            "progress_updated_at": changed_at.isoformat(timespec="seconds"),
            "progress_updated_by": actor_id,
            "last_semantic_patch_actor_id": actor_id,
            "last_semantic_patch_at": changed_at.isoformat(timespec="seconds"),
            "last_semantic_patch_confidence": f"{patch.target_confidence:.2f}",
            "last_semantic_patch_evidence": patch.evidence_text,
            "last_state_linked_update_type": linked_type,
        }
        if patch.temporal_update.get("progress_percent"):
            metadata["progress_percent"] = patch.temporal_update["progress_percent"]
        if patch.temporal_update.get("remaining_work"):
            metadata["remaining_work"] = patch.temporal_update["remaining_work"]
        if patch.temporal_update.get("completion_scope"):
            metadata["completion_scope"] = patch.temporal_update["completion_scope"]
        updated = replace(base, metadata=metadata, updated_at=changed_at)
        self.store.save_proposal(updated)
        self.store.append_event(
            "proposal.changed",
            {
                "proposal": updated,
                "change_body": patch.body,
                "actor_id": actor_id,
                "semantic_patch": patch.to_payload(),
                "change_type": "semantic_progress",
            },
            occurred_at=changed_at,
        )
        return OrchestrationResult(
            proposals=(updated,),
            outbound_messages=(_semantic_direct_update_message(updated, actor_id=actor_id),),
        )

    def handle_feedback(
        self,
        *,
        request_id: str,
        actor_id: str,
        body: str,
        changed_at: datetime,
        temporal_update: dict[str, str] | None = None,
        expected_proposal_id: str = "",
    ) -> OrchestrationResult:
        request = self.store.get_approval_request(request_id)
        if request is None or request.approver_id != actor_id or request.status != "pending":
            return OrchestrationResult()
        if expected_proposal_id and request.proposal_id != expected_proposal_id:
            return OrchestrationResult()
        proposal = self.store.get_proposal(request.proposal_id)
        if proposal is None:
            return OrchestrationResult()

        update = temporal_update if temporal_update is not None else parse_temporal_update(body, reference_date=changed_at.date())
        changed = _apply_temporal_change(proposal, update, actor_id=actor_id, changed_at=changed_at)

        if proposal.metadata.get(WORKFLOW_GROUP_REQUEST_ID_KEY) == request.request_id:
            changed = replace(changed, missing_slots=(), updated_at=changed_at)
            if changed != proposal:
                self.store.save_proposal(changed)
                self.store.append_event(
                    "proposal.changed",
                    {"proposal": changed, "change_body": body, "actor_id": actor_id},
                    occurred_at=changed_at,
                )
            return self._handle_workflow_group_approval(
                request=request,
                proposal=changed,
                approver_id=actor_id,
                accepted=True,
                decided_at=changed_at,
            )

        missing_slots = missing_slots_for_proposal(changed)
        changed = replace(changed, missing_slots=missing_slots, updated_at=changed_at)

        if missing_slots:
            self.store.save_proposal(changed)
            self.store.append_event(
                "proposal.changed",
                {"proposal": changed, "change_body": body, "actor_id": actor_id},
                occurred_at=changed_at,
            )
            if changed.metadata.get("deferred_missing_slots"):
                return OrchestrationResult(
                    proposals=(changed,),
                    approval_requests=(request,),
                    outbound_messages=(
                        OutboundMessage(
                            surface="personal_chat",
                            recipient_id=actor_id,
                            message_type="missing_info_deferred",
                            text=(
                                f"알겠습니다. {changed.title}은(는) 아직 정해지지 않은 정보가 있는 상태로 두고, "
                                "필요한 시점에 다시 확인하겠습니다."
                            ),
                            proposal_id=changed.proposal_id,
                            approval_request_id=request.request_id,
                            card={
                                "proposal_id": changed.proposal_id,
                                "request_id": request.request_id,
                                "missing_slots": ", ".join(changed.missing_slots),
                                "deferred_until": changed.metadata.get("deferred_until", ""),
                            },
                        ),
                    ),
                )
            return OrchestrationResult(
                proposals=(changed,),
                approval_requests=(request,),
                outbound_messages=(
                    missing_info_followup_message(changed, request, now=changed_at),
                ),
            )

        decided_request = replace(request, status="accepted", decided_at=changed_at)
        decision = ApprovalDecision(
            request_id=request.request_id,
            proposal_id=request.proposal_id,
            approver_id=actor_id,
            decision="accepted",
            decided_at=changed_at,
        )
        approvals = tuple(sorted(set((*changed.approvals, actor_id))))
        required = changed.required_approvers or (actor_id,)
        next_status = "approved" if set(required).issubset(approvals) else "awaiting_approval"
        changed = replace(
            changed,
            status=next_status,
            required_approvers=required,
            approvals=approvals,
            updated_at=changed_at,
        )
        self.store.save_approval_request(decided_request)
        self.store.save_approval_decision(decision)
        self.store.append_event("approval.accepted", {"decision": decision}, occurred_at=changed_at)
        self.store.save_proposal(changed)
        self.store.append_event(
            "proposal.changed",
            {"proposal": changed, "change_body": body, "actor_id": actor_id},
            occurred_at=changed_at,
        )
        proposals = [changed]
        outbound: list[OutboundMessage] = []
        if changed.status == "approved":
            self.store.append_event("proposal.approved", {"proposal": changed}, occurred_at=changed_at)
            proposals.extend(self._create_prep_subtasks(changed, now=changed_at))
            outbound.append(
                _team_message(
                    changed,
                    message_type="proposal_approved",
                    text=f"확정했습니다: {changed.title}",
                )
            )
        return OrchestrationResult(
            proposals=tuple(proposals),
            approval_requests=(decided_request,),
            outbound_messages=tuple(outbound),
        )

    def handle_assign(
        self,
        *,
        target_id: str,
        actor_id: str,
        body: str,
        assigned_at: datetime,
    ) -> OrchestrationResult:
        request = self.store.get_approval_request(target_id)
        proposal_id = request.proposal_id if request is not None else target_id
        proposal = self.store.get_proposal(proposal_id)
        assigned_to = _assignee_from_text(body)
        if proposal is None or assigned_to == "":
            return OrchestrationResult(
                outbound_messages=(
                    OutboundMessage(
                        surface="personal_chat",
                        recipient_id=actor_id,
                        message_type="assign_needs_assignee",
                        text=f"담당자를 찾지 못했습니다: {body}",
                        proposal_id=proposal_id,
                    ),
                )
            )

        metadata = {
            **proposal.metadata,
            "last_assign_actor_id": actor_id,
            "last_assign_at": assigned_at.isoformat(timespec="seconds"),
            "previous_assigned_to": proposal.assigned_to,
        }
        reassigned = replace(
            proposal,
            assigned_to=assigned_to,
            metadata=metadata,
            updated_at=assigned_at,
        )
        updated, requests, outbound = apply_assignment_policy(reassigned, actor_id=actor_id, now=assigned_at)
        self.store.save_proposal(updated)
        self.store.append_event(
            "proposal.assigned",
            {"proposal": updated, "actor_id": actor_id},
            occurred_at=assigned_at,
        )
        for new_request in requests:
            self.store.save_approval_request(new_request)
            self.store.append_event("approval.requested", {"request": new_request}, occurred_at=assigned_at)
        if updated.status == "approved":
            self.store.append_event("proposal.approved", {"proposal": updated}, occurred_at=assigned_at)
            generated = self._create_prep_subtasks(updated, now=assigned_at)
        else:
            generated = ()
        return OrchestrationResult(
            proposals=(updated, *generated),
            approval_requests=requests,
            outbound_messages=outbound,
        )

    def handle_complete(
        self,
        *,
        target_id: str,
        actor_id: str,
        completed_at: datetime,
    ) -> OrchestrationResult:
        request = self.store.get_approval_request(target_id)
        proposal_id = request.proposal_id if request is not None else target_id
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return OrchestrationResult()

        updated = replace(
            proposal,
            status="done",
            metadata={
                **proposal.metadata,
                "completed_by": actor_id,
                "completed_at": completed_at.isoformat(timespec="seconds"),
            },
            updated_at=completed_at,
        )
        self.store.save_proposal(updated)
        self.store.append_event("proposal.completed", {"proposal": updated, "actor_id": actor_id}, occurred_at=completed_at)
        return OrchestrationResult(
            proposals=(updated,),
            outbound_messages=(
                _team_message(
                    updated,
                    message_type="proposal_completed",
                    text=f"완료로 표시했습니다: {updated.title}",
                ),
            ),
        )

    def handle_change(
        self,
        *,
        target_id: str,
        actor_id: str,
        body: str,
        changed_at: datetime,
    ) -> OrchestrationResult:
        request = self.store.get_approval_request(target_id)
        proposal_id = request.proposal_id if request is not None else target_id
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return OrchestrationResult()
        if request is not None and request.approver_id != actor_id:
            return OrchestrationResult()

        temporal = parse_temporal_update(body, reference_date=changed_at.date())
        if not temporal:
            return OrchestrationResult(
                outbound_messages=(
                    OutboundMessage(
                        surface="personal_chat",
                        recipient_id=actor_id,
                        message_type="change_needs_date",
                        text=f"변경할 날짜를 찾지 못했습니다: {body}",
                        proposal_id=proposal.proposal_id,
                    ),
                )
            )

        updated = _apply_temporal_change(proposal, temporal, actor_id=actor_id, changed_at=changed_at)
        updated = replace(updated, missing_slots=missing_slots_for_proposal(updated), updated_at=changed_at)
        decided_request: ApprovalRequest | None = None
        if request is not None:
            decided_request = replace(request, status="accepted", decided_at=changed_at)
            decision = ApprovalDecision(
                request_id=request.request_id,
                proposal_id=request.proposal_id,
                approver_id=actor_id,
                decision="accepted",
                decided_at=changed_at,
            )
            self.store.save_approval_request(decided_request)
            self.store.save_approval_decision(decision)
            self.store.append_event("approval.accepted", {"decision": decision}, occurred_at=changed_at)

        approvals = tuple(sorted(set((*updated.approvals, actor_id))))
        next_status = "approved" if set(updated.required_approvers).issubset(approvals) else updated.status
        updated = replace(updated, approvals=approvals, status=next_status, updated_at=changed_at)
        self.store.save_proposal(updated)
        self.store.append_event(
            "proposal.changed",
            {"proposal": updated, "change_body": body, "actor_id": actor_id},
            occurred_at=changed_at,
        )

        outbound: tuple[OutboundMessage, ...] = ()
        generated: tuple[Proposal, ...] = ()
        if updated.status == "approved":
            self.store.append_event("proposal.approved", {"proposal": updated}, occurred_at=changed_at)
            generated = self._create_prep_subtasks(updated, now=changed_at)
            outbound = (
                _team_message(
                    updated,
                    message_type="proposal_approved",
                    text=f"일정을 확정했습니다: {updated.title}",
                ),
            )
        return OrchestrationResult(
            proposals=(updated, *generated),
            approval_requests=() if decided_request is None else (decided_request,),
            outbound_messages=outbound,
        )

    def handle_approval(
        self,
        *,
        request_id: str,
        approver_id: str,
        accepted: bool,
        decided_at: datetime,
    ) -> OrchestrationResult:
        request = self.store.get_approval_request(request_id)
        if request is None or request.approver_id != approver_id or request.status != "pending":
            return OrchestrationResult()

        proposal = self.store.get_proposal(request.proposal_id)
        if proposal is None or proposal.status not in {"awaiting_approval", "posted"}:
            return OrchestrationResult()

        if proposal.metadata.get(WORKFLOW_GROUP_REQUEST_ID_KEY) == request.request_id:
            return self._handle_workflow_group_approval(
                request=request,
                proposal=proposal,
                approver_id=approver_id,
                accepted=accepted,
                decided_at=decided_at,
            )

        decision_value = "accepted" if accepted else "rejected"
        decided_request = replace(request, status=decision_value, decided_at=decided_at)
        decision = ApprovalDecision(
            request_id=request.request_id,
            proposal_id=request.proposal_id,
            approver_id=approver_id,
            decision=decision_value,
            decided_at=decided_at,
        )
        self.store.save_approval_request(decided_request)
        self.store.save_approval_decision(decision)
        self.store.append_event(f"approval.{decision_value}", {"decision": decision}, occurred_at=decided_at)

        if not accepted:
            updated = replace(proposal, status="rejected", updated_at=decided_at)
            self.store.save_proposal(updated)
            self.store.append_event("proposal.rejected", {"proposal": updated}, occurred_at=decided_at)
            return OrchestrationResult(
                proposals=(updated,),
                approval_requests=(decided_request,),
                outbound_messages=(
                    _team_message(
                        updated,
                        message_type="proposal_rejected",
                        text=f"거절되어 보류했습니다: {updated.title}",
                    ),
                ),
            )

        approvals = tuple(sorted(set((*proposal.approvals, approver_id))))
        next_status = "approved" if set(proposal.required_approvers).issubset(approvals) else proposal.status
        updated = replace(proposal, approvals=approvals, status=next_status, updated_at=decided_at)
        self.store.save_proposal(updated)
        outbound: tuple[OutboundMessage, ...] = ()
        generated: tuple[Proposal, ...] = ()
        if next_status == "approved":
            self.store.append_event("proposal.approved", {"proposal": updated}, occurred_at=decided_at)
            generated = self._create_prep_subtasks(updated, now=decided_at)
            outbound = (
                _team_message(
                    updated,
                    message_type="proposal_approved",
                    text=f"승인되어 일정 후보에 반영했습니다: {updated.title}",
                ),
            )
        return OrchestrationResult(
            proposals=(updated, *generated),
            approval_requests=(decided_request,),
            outbound_messages=outbound,
        )

    def _handle_workflow_group_approval(
        self,
        *,
        request: ApprovalRequest,
        proposal: Proposal,
        approver_id: str,
        accepted: bool,
        decided_at: datetime,
    ) -> OrchestrationResult:
        decision_value = "accepted" if accepted else "rejected"
        decided_request = replace(request, status=decision_value, decided_at=decided_at)
        decision = ApprovalDecision(
            request_id=request.request_id,
            proposal_id=request.proposal_id,
            approver_id=approver_id,
            decision=decision_value,
            decided_at=decided_at,
        )
        self.store.save_approval_request(decided_request)
        self.store.save_approval_decision(decision)
        self.store.append_event(f"approval.{decision_value}", {"decision": decision}, occurred_at=decided_at)

        child_ids = _split_csv(proposal.metadata.get(WORKFLOW_GROUP_CHILD_IDS_KEY, ""))
        child_proposals_to_update = [
            child
            for child_id in child_ids
            for child in (self.store.get_proposal(child_id),)
            if child is not None
        ]
        updated: list[Proposal] = []
        if accepted:
            approvals = tuple(sorted(set((*proposal.approvals, approver_id))))
            required = proposal.required_approvers or (approver_id,)
            next_status = "approved" if set(required).issubset(approvals) else "awaiting_approval"
            updated_parent = replace(proposal, approvals=approvals, required_approvers=required, status=next_status, updated_at=decided_at)
            self.store.save_proposal(updated_parent)
            updated.append(updated_parent)
            if updated_parent.status == "approved":
                self.store.append_event("proposal.approved", {"proposal": updated_parent}, occurred_at=decided_at)
            for child in child_proposals_to_update:
                child_required = child.required_approvers or required
                child_approvals = tuple(sorted(set((*child.approvals, approver_id))))
                child_status = "approved" if set(child_required).issubset(child_approvals) else "awaiting_approval"
                updated_child = replace(
                    child,
                    approvals=child_approvals,
                    required_approvers=child_required,
                    status=child_status,
                    updated_at=decided_at,
                )
                self.store.save_proposal(updated_child)
                updated.append(updated_child)
                if updated_child.status == "approved":
                    self.store.append_event(
                        "proposal.approved",
                        {"proposal": updated_child, "workflow_group_id": workflow_group_id(proposal)},
                        occurred_at=decided_at,
                    )
            self.store.append_event(
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
        self.store.save_proposal(updated_parent)
        self.store.append_event("proposal.rejected", {"proposal": updated_parent}, occurred_at=decided_at)
        updated.append(updated_parent)
        for child in child_proposals_to_update:
            updated_child = replace(child, status="rejected", updated_at=decided_at)
            self.store.save_proposal(updated_child)
            self.store.append_event(
                "proposal.rejected",
                {"proposal": updated_child, "workflow_group_id": workflow_group_id(proposal)},
                occurred_at=decided_at,
            )
            updated.append(updated_child)
        self.store.append_event(
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

    def mark_applied(self, proposal_id: str, export_item_id: str, *, applied_at: datetime) -> Proposal | None:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None or proposal.status != "approved":
            return None
        updated = replace(proposal, status="applied", updated_at=applied_at)
        self.store.save_proposal(updated)
        self.store.mark_proposal_applied(proposal_id, export_item_id, applied_at=applied_at)
        self.store.append_event(
            "proposal.applied",
            {"proposal_id": proposal_id, "export_item_id": export_item_id},
            occurred_at=applied_at,
        )
        return updated

    def _ensure_missing_slot_request(
        self,
        proposal: Proposal,
        *,
        actor_id: str,
        now: datetime,
    ) -> tuple[ApprovalRequest, bool]:
        for request in self.store.list_approval_requests(
            proposal_id=proposal.proposal_id,
            approver_id=actor_id,
            status="pending",
        ):
            return request, False
        for request in self.store.list_approval_requests(proposal_id=proposal.proposal_id, status="pending"):
            return request, False
        request = approval_request(proposal.proposal_id, actor_id or proposal.proposer_id or "me", now=now)
        self.store.save_approval_request(request)
        return request, True

    def _create_prep_subtasks(self, proposal: Proposal, *, now: datetime) -> tuple[Proposal, ...]:
        if proposal.kind != "routine" or proposal.metadata.get("needs_prep") != "true":
            return ()
        if any(
            existing.metadata.get("parent_proposal_id") == proposal.proposal_id
            and existing.metadata.get("link_type") == "prep_subtask"
            for existing in self.store.list_proposals()
        ):
            return ()
        occurrence_date = _routine_occurrence_date(proposal)
        if occurrence_date is None:
            return ()
        prep_due = occurrence_date - timedelta(days=1)
        assignee = proposal.proposer_id if proposal.proposer_id in {"me", "teammate"} else "me"
        digest = hashlib.sha1(f"{proposal.proposal_id}:prep:{occurrence_date.isoformat()}".encode("utf-8")).hexdigest()[:12]
        prep = Proposal(
            proposal_id=f"{proposal.proposal_id}/prep/{digest}",
            source_message_id=proposal.source_message_id,
            proposer_id=proposal.proposer_id,
            title=f"{proposal.title} 자료 준비",
            raw_text=f"{proposal.raw_text} / 자료 준비",
            kind="task",
            status="approved",
            assigned_to=assignee,
            task_management_area=proposal.task_management_area,
            discussion_id=proposal.discussion_id,
            message_id=f"{proposal.message_id}/prep",
            required_approvers=(assignee,),
            approvals=(assignee,),
            missing_slots=(),
            due_date=prep_due,
            created_at=now,
            updated_at=now,
            metadata={
                "parent_proposal_id": proposal.proposal_id,
                "link_type": "prep_subtask",
                "routine_occurrence_date": occurrence_date.isoformat(),
                "due_offset_days": "1",
                "materials": proposal.metadata.get("materials", "자료 준비"),
                "source_text_hash": text_hash(proposal.raw_text),
            },
        )
        self.store.save_proposal(prep)
        self.store.append_event("proposal.created", {"proposal": prep, "generated": True}, occurred_at=now)
        self.store.append_event(
            "approval.accepted",
            {"proposal_id": prep.proposal_id, "approver_id": assignee, "auto": True},
            occurred_at=now,
        )
        self.store.append_event("proposal.approved", {"proposal": prep, "generated": True}, occurred_at=now)
        return (prep,)


def _apply_temporal_change(
    proposal: Proposal,
    temporal: dict[str, str],
    *,
    actor_id: str,
    changed_at: datetime,
) -> Proposal:
    metadata = {
        **proposal.metadata,
        "last_change_actor_id": actor_id,
        "last_change_at": changed_at.isoformat(timespec="seconds"),
    }
    due_date = proposal.due_date
    scheduled_date = proposal.scheduled_date
    title = proposal.title
    corrected_title = (temporal.get("title") or temporal.get("corrected_title") or "").strip()
    if corrected_title and corrected_title != proposal.title:
        metadata.setdefault("previous_title", proposal.title)
        title = corrected_title
    resolves_date_window = any(temporal.get(key) for key in ("due_date", "scheduled_date", "date_window_start"))
    if resolves_date_window:
        for key in ("date_window_start", "date_window_end", "date_window_label", "needs_exact_date"):
            if key in temporal:
                continue
            metadata.pop(key, None)
        if proposal.metadata.get("date_window_start") and proposal.metadata.get("date_window_end"):
            metadata["resolved_date_window_start"] = proposal.metadata["date_window_start"]
            metadata["resolved_date_window_end"] = proposal.metadata["date_window_end"]
            metadata["resolved_date_window_label"] = proposal.metadata.get("date_window_label", "")
    if temporal.get("due_date"):
        due_date = datetime.fromisoformat(temporal["due_date"]).date()
        scheduled_date = None
    if temporal.get("scheduled_date"):
        scheduled_date = datetime.fromisoformat(temporal["scheduled_date"]).date()
        due_date = None
    if temporal.get("date_window_start"):
        metadata.update(
            {
                "date_window_start": temporal["date_window_start"],
                "date_window_end": temporal.get("date_window_end", ""),
                "date_window_label": temporal.get("date_window_label", ""),
                "needs_exact_date": temporal.get("needs_exact_date", "true"),
            }
        )
    for key in (
        "participants",
        "external_owner",
        "external_participants",
        "participant_label",
        "attendees",
        "location",
        "location_optional",
        "materials",
        "needs_prep",
        "needs_exact_time",
    ):
        if temporal.get(key):
            metadata[key] = temporal[key]
    if temporal.get("defer_missing_slots"):
        deferred_slots = csv_dedupe(temporal["defer_missing_slots"])
        if deferred_slots:
            metadata["deferred_missing_slots"] = ",".join(deferred_slots)
            metadata["deferred_reason"] = "not_decided"
            metadata["deferred_at"] = changed_at.isoformat(timespec="seconds")
            metadata["deferred_until"] = temporal.get("deferred_until") or default_deferred_until(
                scheduled_date or due_date,
                changed_at=changed_at,
            )
            metadata["deferred_reminder_cadence_hours"] = temporal.get("deferred_reminder_cadence_hours", "2")

    kind: ProposalKind = proposal.kind
    if scheduled_date is not None:
        kind = "event"
    elif due_date is not None and kind == "question":
        kind = "task"

    return replace(
        proposal,
        title=title,
        kind=kind,
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=temporal.get("time_window", proposal.time_window),
        missing_slots=(),
        metadata=metadata,
        updated_at=changed_at,
    )


def _meaningful_semantic_update(update: dict[str, str]) -> bool:
    return any(
        update.get(key)
        for key in (
            "due_date",
            "scheduled_date",
            "date_window_start",
            "time_window",
            "participants",
            "external_owner",
            "external_participants",
            "participant_label",
            "attendees",
            "location",
            "location_optional",
            "materials",
            "needs_prep",
            "needs_exact_time",
            "defer_missing_slots",
            "status",
            "progress_status",
            "progress_percent",
            "remaining_work",
            "completion_scope",
            "title",
            "corrected_title",
            "semantic_update_type",
        )
    )


def _has_temporal_or_slot_change(update: dict[str, str]) -> bool:
    return any(
        update.get(key)
        for key in (
            "due_date",
            "scheduled_date",
            "date_window_start",
            "time_window",
            "participants",
            "external_owner",
            "external_participants",
            "participant_label",
            "attendees",
            "location",
            "location_optional",
            "materials",
            "needs_prep",
            "needs_exact_time",
            "defer_missing_slots",
            "title",
            "corrected_title",
        )
    )


_SCOPED_COMPLETION_SCOPES = {
    "preparation",
    "prep",
    "materials",
    "material",
    "subtask",
    "deliverable",
    "followup",
    "follow-up",
}


def _is_scoped_progress_completion(update: dict[str, str]) -> bool:
    scope = update.get("completion_scope", "").strip().lower()
    return (
        update.get("semantic_update_type") == "completion"
        and update.get("status") == "done"
        and bool(update.get("progress_status"))
        and scope in _SCOPED_COMPLETION_SCOPES
    )


def _progress_update_from_scoped_completion(update: dict[str, str]) -> dict[str, str]:
    adjusted = dict(update)
    adjusted.pop("status", None)
    adjusted["semantic_update_type"] = "progress"
    adjusted["progress_status"] = adjusted.get("progress_status") or "complete"
    return adjusted


def _is_confirmation_update(update: dict[str, str]) -> bool:
    return update.get("semantic_update_type") == "confirmation" or update.get("status") == "confirmed"


def _requires_actionable_direct_patch(update: dict[str, str]) -> bool:
    if _is_scoped_progress_completion(update):
        return False
    update_type = update.get("semantic_update_type")
    if update_type in {"progress", "confirmation", "correction"}:
        return False
    return bool(update.get("status") == "done" or update_type in {"completion", "deferral"})


def _has_correction_slot_update(update: dict[str, str]) -> bool:
    return any(update.get(key) for key in _SEMANTIC_CORRECTION_KEYS)


def _semantic_update_shape_rejection(update: dict[str, str]) -> str:
    status = update.get("status", "")
    if status and status not in {"done", "confirmed"}:
        return "unsupported_status_patch"
    update_type = update.get("semantic_update_type", "")
    if not update_type:
        return ""
    if update_type not in {"completion", "progress", "deferral", "confirmation", "correction"}:
        return "invalid_semantic_update_type"
    if update_type == "correction" and status:
        return "correction_must_not_set_status"
    if update_type == "correction" and not _has_correction_slot_update(update):
        return "correction_requires_slot_update"
    if status == "confirmed" and update_type != "confirmation":
        return "confirmed_status_requires_confirmation_type"
    if update_type == "completion" and update.get("status") != "done":
        return "completion_requires_done_status"
    if update_type == "progress" and not update.get("progress_status"):
        return "progress_requires_progress_status"
    if update_type == "deferral" and not any(update.get(key) for key in ("due_date", "scheduled_date", "time_window")):
        return "deferral_requires_temporal_update"
    if update_type == "confirmation" and not (
        status == "confirmed" or any(update.get(key) for key in ("due_date", "scheduled_date", "time_window", "location"))
    ):
        return "confirmation_requires_confirmed_status_or_slot"
    return ""


def _semantic_direct_update_message(proposal: Proposal, *, actor_id: str) -> OutboundMessage:
    if proposal.metadata.get("last_state_linked_update_type") == "semantic_completion":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_completed",
            text=f"완료로 표시했습니다: {proposal.title}",
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "completed_at": proposal.metadata.get("completed_at", ""),
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "semantic_progress":
        remaining = proposal.metadata.get("remaining_work", "")
        suffix = f"\n남은 일: {remaining}" if remaining else ""
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_progress_updated",
            text=f"진행 상황을 기록했습니다: {proposal.title}{suffix}",
            proposal_id=proposal.proposal_id,
            card={
                "dedupe_key": _progress_update_dedupe_key(
                    proposal,
                    actor_id=actor_id,
                    message_type="proposal_progress_updated",
                ),
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "progress_status": proposal.metadata.get("progress_status", ""),
                "progress_percent": proposal.metadata.get("progress_percent", ""),
                "remaining_work": remaining,
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "semantic_confirmation":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        detail = " ".join(item for item in (when, proposal.time_window) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_confirmed",
            text=f"일정 확정을 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                "time": proposal.time_window,
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "semantic_deferral":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        detail = " ".join(item for item in (when, proposal.time_window) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_deferred",
            text=f"일정을 미뤄 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                "time": proposal.time_window,
            },
        )
    details = []
    if proposal.scheduled_date:
        details.append(proposal.scheduled_date.isoformat())
    elif proposal.due_date:
        details.append(proposal.due_date.isoformat())
    if proposal.time_window:
        details.append(proposal.time_window)
    if proposal.metadata.get("location"):
        details.append(proposal.metadata["location"])
    suffix = f" ({' · '.join(details)})" if details else ""
    missing = f"\n아직 필요한 정보: {', '.join(proposal.missing_slots)}" if proposal.missing_slots else ""
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="semantic_patch_applied",
        text=f"기존 작업에 반영했습니다: {proposal.title}{suffix}{missing}",
        proposal_id=proposal.proposal_id,
        card={
            "proposal_id": proposal.proposal_id,
            "title": proposal.title,
            "status": proposal.status,
            "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
            "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
            "time": proposal.time_window,
            "location": proposal.metadata.get("location", ""),
            "missing_slots": ", ".join(proposal.missing_slots),
        },
    )


_RELATED_COMPLETION_STOPWORDS = frozenset(
    {
        "일정",
        "결정",
        "진행",
        "예정",
        "확정",
        "완료",
        "관련",
        "위에서",
        "말한",
        "해당",
        "시각",
        "회의",
        "미팅",
        "후속",
        "자료",
        "보고",
        "정리",
        "the",
        "and",
        "for",
        "with",
    }
)


def _same_scheduled_commitment_slot(candidate: Proposal, completed: Proposal) -> bool:
    if candidate.kind != "event" or candidate.status not in {"approved", "applied", "awaiting_approval"}:
        return False
    if candidate.scheduled_date is None or candidate.scheduled_date != completed.scheduled_date:
        return False
    if candidate.time_window and completed.time_window:
        return time_sort_minutes(candidate.time_window) == time_sort_minutes(completed.time_window)
    return True


def _has_related_completion_signal(candidate: Proposal, completed: Proposal, patch: ProposalPatch) -> bool:
    if _explicitly_linked(candidate, completed):
        return True
    candidate_tokens = _completion_topic_tokens(
        candidate.title,
        candidate.raw_text,
        *_metadata_topic_values(candidate),
    )
    completed_tokens = _completion_topic_tokens(
        completed.title,
        completed.raw_text,
        patch.body,
        patch.evidence_text,
        *_metadata_topic_values(completed),
    )
    if len(candidate_tokens & completed_tokens) >= 2:
        return True
    if candidate.metadata.get("decision_pending") == "true" or candidate.metadata.get("date_resolution_policy"):
        return bool(candidate_tokens & completed_tokens) and _shares_participant_hint(candidate, completed)
    return False


def _explicitly_linked(candidate: Proposal, completed: Proposal) -> bool:
    return any(
        value == completed.proposal_id
        for value in (
            candidate.metadata.get("parent_proposal_id", ""),
            candidate.metadata.get("linked_completion_source_proposal_id", ""),
        )
    ) or any(
        value == candidate.proposal_id
        for value in (
            completed.metadata.get("parent_proposal_id", ""),
            completed.metadata.get("linked_completion_source_proposal_id", ""),
        )
    )


def _metadata_topic_values(proposal: Proposal) -> tuple[str, ...]:
    keys = (
        "materials",
        "external_participants",
        "participant_label",
        "external_owner",
        "progress_note",
        "previous_title",
        "last_semantic_patch_evidence",
    )
    return tuple(proposal.metadata.get(key, "") for key in keys if proposal.metadata.get(key))


def _completion_topic_tokens(*texts: str) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        for token in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
            if token.isdigit() or len(token) < 2 or token in _RELATED_COMPLETION_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def _shares_participant_hint(candidate: Proposal, completed: Proposal) -> bool:
    candidate_people = _completion_topic_tokens(
        candidate.metadata.get("participants", ""),
        candidate.metadata.get("external_participants", ""),
        candidate.metadata.get("participant_label", ""),
    )
    completed_people = _completion_topic_tokens(
        completed.metadata.get("participants", ""),
        completed.metadata.get("external_participants", ""),
        completed.metadata.get("participant_label", ""),
    )
    return bool(candidate_people & completed_people)


def _state_update_message(proposal: Proposal, *, actor_id: str) -> OutboundMessage:
    if proposal.metadata.get("last_state_linked_update_type") == "natural_completion":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_completed",
            text=f"완료로 표시했습니다: {proposal.title}",
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "completed_at": proposal.metadata.get("completed_at", ""),
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "natural_progress":
        remaining = proposal.metadata.get("remaining_work", "")
        suffix = f"\n남은 일: {remaining}" if remaining else ""
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_progress_updated",
            text=f"진행 상황을 기록했습니다: {proposal.title}{suffix}",
            proposal_id=proposal.proposal_id,
            card={
                "dedupe_key": _progress_update_dedupe_key(
                    proposal,
                    actor_id=actor_id,
                    message_type="proposal_progress_updated",
                ),
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "progress_status": proposal.metadata.get("progress_status", ""),
                "progress_percent": proposal.metadata.get("progress_percent", ""),
                "remaining_work": remaining,
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "natural_deferral":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        detail = " ".join(item for item in (when, proposal.time_window) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_deferred",
            text=f"일정을 미뤄 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                "time": proposal.time_window,
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "conflict_resolution_applied":
        action = proposal.metadata.get("conflict_resolution_action", "")
        existing = proposal.metadata.get("conflict_resolved_existing_titles", "")
        if action in {"not_attending_existing", "cancel_existing"}:
            detail = f"‘{existing}’은 불참/제외 처리하고, " if existing else ""
            text = f"반영했습니다. {detail}{proposal.title} 일정은 확정했습니다."
        elif action == "keep_both":
            text = f"반영했습니다. 기존 일정은 유지하고, {proposal.title} 일정도 확정했습니다."
        else:
            text = f"반영했습니다. {proposal.title} 일정 충돌 처리를 완료했습니다."
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="conflict_resolved",
            text=text,
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "conflict_resolution_action": action,
                "conflict_resolved_existing_titles": existing,
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "conflict_resolution_deferred":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="conflict_resolution_deferred",
            text=f"알겠습니다. {proposal.title} 일정 충돌은 일단 보류 상태로 두겠습니다.",
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "missing_info_resolved":
        when = (
            proposal.scheduled_date.isoformat()
            if proposal.scheduled_date
            else proposal.due_date.isoformat()
            if proposal.due_date
            else ""
        )
        location = proposal.metadata.get("location", "")
        detail = " ".join(item for item in (when, proposal.time_window, location) if item)
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="proposal_approved" if proposal.status == "approved" else "proposal_changed",
            text=f"일정 정보를 반영했습니다: {proposal.title}" + (f" ({detail})" if detail else ""),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "status": proposal.status,
                "scheduled_date": when,
                "time": proposal.time_window,
                "location": location,
                "missing_slots": ", ".join(proposal.missing_slots),
            },
        )
    if proposal.metadata.get("last_state_linked_update_type") == "missing_info_deferred":
        return OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="missing_info_deferred",
            text=(
                f"알겠습니다. {proposal.title}은(는) 아직 정해지지 않은 정보가 있는 상태로 두고, "
                "필요한 시점에 다시 확인하겠습니다."
            ),
            proposal_id=proposal.proposal_id,
            card={
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "missing_slots": ", ".join(proposal.missing_slots),
                "deferred_until": proposal.metadata.get("deferred_until", ""),
            },
        )
    return _team_message(
        proposal,
        message_type="proposal_changed",
        text=f"일정 업데이트를 반영했습니다: {proposal.title}",
    )


def _progress_update_dedupe_key(proposal: Proposal, *, actor_id: str, message_type: str) -> str:
    token_source = "|".join(
        (
            proposal.proposal_id,
            proposal.metadata.get("progress_updated_at", ""),
            proposal.metadata.get("last_semantic_patch_at", ""),
            proposal.metadata.get("last_semantic_patch_evidence", ""),
            proposal.metadata.get("progress_note", ""),
            proposal.metadata.get("remaining_work", ""),
            proposal.time_window,
            proposal.due_date.isoformat() if proposal.due_date else "",
            proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
        )
    )
    token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:16]
    return f"slack-outbound/{actor_id}/{message_type}/{proposal.proposal_id}/{token}"


def _agent_clarification_message(
    *,
    recipient_id: str,
    prompt: str,
    proposal_id: str,
    missing_slots: tuple[str, ...],
) -> OutboundMessage:
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=recipient_id,
        message_type="agent_clarification",
        text=prompt,
        proposal_id=proposal_id,
        card={
            "proposal_id": proposal_id,
            "missing_slots": ", ".join(missing_slots),
        },
    )


def _patch_rejection_message(patch: ProposalPatch, *, actor_id: str, reason: str) -> OutboundMessage:
    if reason == "low_target_confidence":
        text = (
            "어느 작업에 반영할지 확신이 낮아 자동으로 바꾸지 않았습니다.\n"
            f"제가 이해한 근거: {patch.evidence_text or patch.body}\n"
            "어떤 작업을 말하는지 한 번 더 알려주세요."
        )
    elif reason == "agent_requested_clarification":
        slots = ", ".join(patch.missing_slots) if patch.missing_slots else "추가 정보"
        text = (
            "아직 바로 반영하기에는 정보가 부족합니다.\n"
            f"확인이 필요한 항목: *{slots}*\n"
            f"제가 이해한 근거: {patch.evidence_text or patch.body}"
        )
    else:
        text = (
            "이 답변을 안전하게 반영하지 않았습니다.\n"
            f"사유: {reason}\n"
            "대상 작업이나 승인 요청을 다시 확인해주세요."
        )
    return OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="agent_patch_rejected",
        text=text,
        proposal_id=patch.proposal_id,
        approval_request_id=patch.request_id,
        card={
            "proposal_id": patch.proposal_id,
            "request_id": patch.request_id,
            "reason": reason,
            "target_confidence": f"{patch.target_confidence:.2f}",
            "evidence_text": patch.evidence_text,
        },
    )


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


def _decision_contains_workflow_batch(decision: OperatingAgentDecision) -> bool:
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


def _recent_conversation_from_events(
    events,
    *,
    chat_id: str,
    sender_id: str,
    limit: int = 8,
    max_chars: int = 600,
) -> tuple[dict, ...]:
    """Reconstruct recent DM turns (user + bot) from the audit log.

    Gives the semantic agent the prior conversation so it can resolve a reply
    against context (e.g. an affirmative answer to the bot's own pending
    question) instead of guessing the target. Pure context, not a rule.
    """

    turns: list[dict] = []
    for event in events:
        etype = event.get("type")
        payload = event.get("payload") or {}
        msg = payload.get("message") or {}
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        if etype == "message.received" and msg.get("chat_id") == chat_id:
            turns.append({"role": "user", "text": text[:max_chars]})
        elif etype == "slack.message.sent" and payload.get("recipient_id") == sender_id:
            turns.append({"role": "assistant", "text": text[:max_chars]})
    return tuple(turns[-limit:])


def _is_slack_notification_candidate_message(message: IncomingMessage) -> bool:
    return message.visibility == "team" and message.message_id.startswith("slack/")


_TRUSTED_SEMANTIC_DECISION_SOURCES = {
    "claude_code_cli",
    "codex_cli",
    "openai_responses",
}


def _should_run_state_linked_fallback(message: IncomingMessage, decision: OperatingAgentDecision) -> bool:
    if decision.action != "no_action":
        return False
    if _is_slack_notification_candidate_message(message):
        return False
    return decision.source not in _TRUSTED_SEMANTIC_DECISION_SOURCES


def _routine_occurrence_date(proposal: Proposal) -> date | None:
    raw = proposal.metadata.get("next_occurrence_date") or proposal.metadata.get("routine_occurrence_date")
    if raw:
        return date.fromisoformat(raw)
    if proposal.scheduled_date is not None:
        return proposal.scheduled_date
    return None

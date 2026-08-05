from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Iterable

from .commands import parse_chat_command, parse_instance_probe
from .command_router import (
    handle_command_message as _route_command_message,
    handle_instance_probe as _route_instance_probe,
    _assignee_from_text,
)
from .conflict_policy import cancel_prep_subtasks, recompute_missing_slots
from .discussion_adapter import parse_temporal_update
from .domain import (
    Actor,
    ApprovalRequest,
    IncomingMessage,
    OrchestrationResult,
    OutboundMessage,
    Proposal,
)
from .approval_policy import (
    apply_assignment_policy,
    approval_request,
    team_message as _team_message,
)
from .approval_flow import (
    approved_followthrough,
    promote_if_fully_approved,
    record_decision,
)
from .operating_agent import (
    DirectResponse,
    TeamTaskOperatingAgent,
    OperatingAgentDecision,
    RuleBasedTeamTaskOperatingAgent,
    decision_to_payload,
)
from .prep_subtasks import build_prep_subtasks
from .pending_info import missing_info_followup_message
from .semantic_context import recent_conversation_from_events
from .update_messages import (
    _agent_clarification_message,
    _agent_direct_response_message,
    _state_update_message,
)
from .relations import (
    COMPLETED_AT_KEY,
    DEFERRED_MISSING_SLOTS_KEY,
    DEFERRED_UNTIL_KEY,
    LINK_PREP_SUBTASK,
    LINK_TYPE_KEY,
    NEEDS_PREP_KEY,
    WORKFLOW_GROUP_REQUEST_ID_KEY,
)
from .store import TeamTaskStore
from .task_reconciler import apply_state_linked_update, _should_run_state_linked_fallback
from .proposal_intake import ProposalIntake
from .semantic_patch_service import (
    MIN_SEMANTIC_TARGET_CONFIDENCE,
    SemanticPatchService,
    _apply_temporal_change,
)
from .workflow_batch import (
    WorkflowBatchService,
    decision_contains_workflow_batch,
)


DEFAULT_ACTORS = (
    Actor(actor_id="me", display_name="나", aliases=("나", "엄마", "me")),
    Actor(actor_id="teammate", display_name="팀원", aliases=("팀원", "팀원", "teammate")),
)

# Re-exported from semantic_patch_service so existing references to
# orchestrator.MIN_SEMANTIC_TARGET_CONFIDENCE keep resolving after the C6 extraction.
__all__ = ["TeamTaskOrchestrator", "DEFAULT_ACTORS", "MIN_SEMANTIC_TARGET_CONFIDENCE"]


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
        self._workflow_batch = WorkflowBatchService(
            self.store,
            create_prep_subtasks=self._create_prep_subtasks,
            persist_graph_normalization=self._persist_graph_normalization,
        )
        self._proposal_intake = ProposalIntake(
            self.store,
            persist_graph_normalization=self._persist_graph_normalization,
            prepare_standalone_child=self._workflow_batch.prepare_standalone_child,
            create_prep_subtasks=self._create_prep_subtasks,
        )
        self._patch_service = SemanticPatchService(
            self.store,
            route_approval=self.handle_approval,
            route_feedback=self.handle_feedback,
            ensure_missing_slot_request=self._ensure_missing_slot_request,
        )

    def handle_message(self, message: IncomingMessage) -> OrchestrationResult:
        if self.store.has_message(message.message_id):
            return OrchestrationResult(ignored_duplicate=True)

        self.store.record_message(message)
        self.store.append_event("message.received", {"message": message}, occurred_at=message.received_at)

        instance_probe = parse_instance_probe(message.text)
        if instance_probe is not None:
            return _route_instance_probe(instance_probe, message, store=self.store)

        command = parse_chat_command(message.text)
        if command is not None:
            return _route_command_message(
                command,
                message,
                store=self.store,
                handle_approval=self.handle_approval,
                handle_change=self.handle_change,
                handle_assign=self.handle_assign,
                handle_complete=self.handle_complete,
            )

        existing_proposals = self.store.list_proposals()
        pending_approval_requests = self.store.list_approval_requests(
            approver_id=message.sender_id,
            status="pending",
        )
        message = replace(
            message,
            recent_conversation=recent_conversation_from_events(
                self.store.read_recent_events(),
                chat_id=message.chat_id,
                sender_id=message.sender_id,
                current_message=message,
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
        outbound: list[OutboundMessage] = list(self._validated_direct_response_messages(message, decision))

        if decision.action in {"apply_feedback", "create_proposals"} and decision.proposal_patches:
            feedback_result = self._patch_service.apply_patches(
                decision.proposal_patches,
                actor_id=message.sender_id,
                changed_at=message.received_at,
            )
            proposals.extend(feedback_result.proposals)
            requests.extend(feedback_result.approval_requests)
            outbound.extend(feedback_result.outbound_messages)
            if decision.action == "apply_feedback" and not decision.proposal_drafts:
                if _should_reinterpret_rejected_feedback_as_new_work(feedback_result):
                    outbound = [
                        message
                        for message in outbound
                        if message.card.get("reason") != "target_mismatch_new_work"
                    ]
                    self.store.append_event(
                        "agent.feedback_reinterpreted_as_new_work",
                        {
                            "message_id": message.message_id,
                            "reason": "target_mismatch_new_work",
                        },
                        occurred_at=message.received_at,
                    )
                    decision = RuleBasedTeamTaskOperatingAgent().decide(
                        message,
                        pending_approval_requests=(),
                        pending_proposals=self.store.list_proposals(),
                    )
                    self.store.append_event(
                        "agent.decision.created",
                        {"decision": decision_to_payload(decision), "fallback_after": "target_mismatch_new_work"},
                        occurred_at=message.received_at,
                    )
                    if not decision.proposal_drafts:
                        return OrchestrationResult(
                            proposals=tuple(proposals),
                            approval_requests=tuple(requests),
                            outbound_messages=tuple(outbound),
                        )
                else:
                    return OrchestrationResult(
                        proposals=tuple(proposals),
                        approval_requests=tuple(requests),
                        outbound_messages=tuple(outbound),
                    )
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
            if message.visibility == "private" and "[요청]" in message.text and not outbound:
                self.store.append_event(
                    "agent.explicit_request.unhandled",
                    {
                        "message_id": message.message_id,
                        "decision_source": decision.source,
                        "reason": "tagged_request_returned_no_action",
                    },
                    occurred_at=message.received_at,
                )
                outbound.append(
                    OutboundMessage(
                        surface="personal_chat",
                        recipient_id=message.sender_id,
                        message_type="explicit_request_unhandled",
                        text=(
                            "요청 내용을 처리할 구조화된 결과를 만들지 못했습니다. "
                            "상태를 임의로 바꾸지는 않았습니다. 적용할 항목과 원하는 변경을 조금 더 구체적으로 알려주세요."
                        ),
                        card={
                            "reason": "tagged_request_returned_no_action",
                            "decision_source": decision.source,
                        },
                    )
                )
            return OrchestrationResult(
                proposals=tuple(proposals),
                approval_requests=tuple(requests),
                outbound_messages=tuple(outbound),
            )
        if decision.action == "respond":
            return OrchestrationResult(outbound_messages=tuple(outbound))

        existing_proposals = self.store.list_proposals()
        if decision.proposal_drafts and decision_contains_workflow_batch(decision):
            workflow_result = self._workflow_batch.create_from_drafts(
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

        intake_result = self._proposal_intake.create_from_drafts(
            decision.proposal_drafts,
            message=message,
            existing_proposals=existing_proposals,
        )
        proposals.extend(intake_result.proposals)
        requests.extend(intake_result.approval_requests)
        outbound.extend(intake_result.outbound_messages)

        return OrchestrationResult(
            proposals=tuple(proposals),
            approval_requests=tuple(requests),
            outbound_messages=tuple(outbound),
        )

    def _validated_direct_response_messages(
        self,
        message: IncomingMessage,
        decision: OperatingAgentDecision,
    ) -> tuple[OutboundMessage, ...]:
        outbound: list[OutboundMessage] = []
        for response in decision.direct_responses:
            reason = self._direct_response_rejection_reason(message, response)
            if reason:
                self.store.append_event(
                    "agent.direct_response.rejected",
                    {
                        "message_id": message.message_id,
                        "recipient_id": response.recipient_id,
                        "proposal_id": response.proposal_id,
                        "request_id": response.request_id,
                        "reason": reason,
                    },
                    occurred_at=message.received_at,
                )
                continue
            outbound.append(_agent_direct_response_message(response))
            self.store.append_event(
                "agent.direct_response.accepted",
                {
                    "message_id": message.message_id,
                    "recipient_id": response.recipient_id,
                    "proposal_id": response.proposal_id,
                    "request_id": response.request_id,
                    "response_type": response.response_type,
                    "interaction_label": response.interaction_label,
                    "evidence_text": response.evidence_text,
                    "confidence": response.confidence,
                },
                occurred_at=message.received_at,
            )
        return tuple(outbound)

    def _direct_response_rejection_reason(self, message: IncomingMessage, response: DirectResponse) -> str:
        if message.visibility != "private":
            return "private_surface_required"
        if response.recipient_id != message.sender_id:
            return "recipient_mismatch"
        if response.confidence < MIN_SEMANTIC_TARGET_CONFIDENCE:
            return "low_confidence"
        if not response.text.strip() or len(response.text) > 4000:
            return "invalid_text"
        proposal = self.store.get_proposal(response.proposal_id) if response.proposal_id else None
        if response.proposal_id:
            if proposal is None:
                return "proposal_not_found"
            if not (
                proposal.proposer_id == message.sender_id
                or proposal.assigned_to == message.sender_id
                or message.sender_id in proposal.required_approvers
            ):
                return "proposal_access_denied"
        if response.request_id:
            request = self.store.get_approval_request(response.request_id)
            if request is None:
                return "request_not_found"
            if request.approver_id != message.sender_id:
                return "request_access_denied"
            if response.proposal_id and request.proposal_id != response.proposal_id:
                return "request_proposal_mismatch"
        return ""

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

    def _state_linked_fallback_after_agent(
        self,
        message: IncomingMessage,
        decision: OperatingAgentDecision,
    ) -> tuple[Proposal, ...]:
        if not _should_run_state_linked_fallback(message, decision):
            return ()
        return apply_state_linked_update(self.store, message, updated_at=message.received_at)

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
            return self._workflow_batch.handle_group_approval(
                request=request,
                proposal=changed,
                approver_id=actor_id,
                accepted=True,
                decided_at=changed_at,
            )

        missing_slots = recompute_missing_slots(changed)
        changed = replace(changed, missing_slots=missing_slots, updated_at=changed_at)

        if missing_slots:
            self.store.save_proposal(changed)
            self.store.append_event(
                "proposal.changed",
                {"proposal": changed, "change_body": body, "actor_id": actor_id},
                occurred_at=changed_at,
            )
            if changed.metadata.get(DEFERRED_MISSING_SLOTS_KEY):
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
                                DEFERRED_UNTIL_KEY: changed.metadata.get(DEFERRED_UNTIL_KEY, ""),
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

        decided_request, _decision = record_decision(
            self.store,
            request,
            approver_id=actor_id,
            accepted=True,
            decided_at=changed_at,
        )
        changed = promote_if_fully_approved(
            changed,
            actor_id,
            fallback_status="awaiting_approval",
            required_override=changed.required_approvers or (actor_id,),
            write_required=True,
            decided_at=changed_at,
        )
        self.store.save_proposal(changed)
        self.store.append_event(
            "proposal.changed",
            {"proposal": changed, "change_body": body, "actor_id": actor_id},
            occurred_at=changed_at,
        )
        proposals = [changed]
        outbound: list[OutboundMessage] = []
        if changed.status == "approved":
            proposals.extend(
                approved_followthrough(
                    self.store,
                    changed,
                    now=changed_at,
                    create_prep_subtasks=self._create_prep_subtasks,
                )
            )
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
            generated = approved_followthrough(
                self.store,
                updated,
                now=assigned_at,
                create_prep_subtasks=self._create_prep_subtasks,
            )
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
                COMPLETED_AT_KEY: completed_at.isoformat(timespec="seconds"),
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
        missing_slots = recompute_missing_slots(updated)
        updated = replace(updated, missing_slots=missing_slots, updated_at=changed_at)

        if missing_slots:
            self.store.save_proposal(updated)
            self.store.append_event(
                "proposal.changed",
                {"proposal": updated, "change_body": body, "actor_id": actor_id},
                occurred_at=changed_at,
            )
            pending_request = request
            if pending_request is None:
                pending_request, _ = self._ensure_missing_slot_request(
                    updated, actor_id=actor_id, now=changed_at
                )
            return OrchestrationResult(
                proposals=(updated,),
                approval_requests=(pending_request,),
                outbound_messages=(
                    missing_info_followup_message(updated, pending_request, now=changed_at),
                ),
            )

        decided_request: ApprovalRequest | None = None
        if request is not None:
            decided_request, _decision = record_decision(
                self.store,
                request,
                approver_id=actor_id,
                accepted=True,
                decided_at=changed_at,
            )

        updated = promote_if_fully_approved(
            updated,
            actor_id,
            fallback_status=updated.status,
            decided_at=changed_at,
        )
        self.store.save_proposal(updated)
        self.store.append_event(
            "proposal.changed",
            {"proposal": updated, "change_body": body, "actor_id": actor_id},
            occurred_at=changed_at,
        )

        outbound: tuple[OutboundMessage, ...] = ()
        generated: tuple[Proposal, ...] = ()
        if updated.status == "approved":
            generated = approved_followthrough(
                self.store,
                updated,
                now=changed_at,
                create_prep_subtasks=self._create_prep_subtasks,
            )
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
            return self._workflow_batch.handle_group_approval(
                request=request,
                proposal=proposal,
                approver_id=approver_id,
                accepted=accepted,
                decided_at=decided_at,
            )

        decided_request, _decision = record_decision(
            self.store,
            request,
            approver_id=approver_id,
            accepted=accepted,
            decided_at=decided_at,
        )

        if not accepted:
            updated = replace(
                proposal,
                status="rejected",
                missing_slots=(),
                required_approvers=(),
                approvals=(),
                updated_at=decided_at,
            )
            self.store.save_proposal(updated)
            self.store.append_event("proposal.rejected", {"proposal": updated}, occurred_at=decided_at)
            cascaded = cancel_prep_subtasks(
                self.store,
                updated.proposal_id,
                now=decided_at,
                reason="parent_rejected",
            )
            return OrchestrationResult(
                proposals=(updated, *cascaded),
                approval_requests=(decided_request,),
                outbound_messages=(
                    _team_message(
                        updated,
                        message_type="proposal_rejected",
                        text=f"거절되어 보류했습니다: {updated.title}",
                    ),
                ),
            )

        updated = promote_if_fully_approved(
            proposal,
            approver_id,
            fallback_status=proposal.status,
            decided_at=decided_at,
        )
        self.store.save_proposal(updated)
        outbound: tuple[OutboundMessage, ...] = ()
        generated: tuple[Proposal, ...] = ()
        if updated.status == "approved":
            generated = approved_followthrough(
                self.store,
                updated,
                now=decided_at,
                create_prep_subtasks=self._create_prep_subtasks,
            )
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
        candidate = approval_request(proposal.proposal_id, actor_id or proposal.proposer_id or "me", now=now)
        existing = self.store.get_approval_request(candidate.request_id)
        if existing is not None and existing.status != "pending":
            # The deterministic id (sha1(proposal_id:approver_id)) collides with a
            # request that was already decided. Never resurrect that terminal record;
            # surface it as-is so the proposal keeps its prior decision.
            return existing, False
        self.store.save_approval_request(candidate)
        return candidate, True

    def _create_prep_subtasks(self, proposal: Proposal, *, now: datetime) -> tuple[Proposal, ...]:
        if proposal.kind != "routine" or proposal.metadata.get(NEEDS_PREP_KEY) != "true":
            return ()
        if any(
            existing.metadata.get("parent_proposal_id") == proposal.proposal_id
            and existing.metadata.get(LINK_TYPE_KEY) == LINK_PREP_SUBTASK
            for existing in self.store.list_proposals()
        ):
            return ()
        prep_subtasks = build_prep_subtasks(proposal, now=now)
        for prep in prep_subtasks:
            assignee = prep.assigned_to
            self.store.save_proposal(prep)
            self.store.append_event("proposal.created", {"proposal": prep, "generated": True}, occurred_at=now)
            self.store.append_event(
                "approval.accepted",
                {"proposal_id": prep.proposal_id, "approver_id": assignee, "auto": True},
                occurred_at=now,
            )
            self.store.append_event("proposal.approved", {"proposal": prep, "generated": True}, occurred_at=now)
        return prep_subtasks


def _should_reinterpret_rejected_feedback_as_new_work(result: OrchestrationResult) -> bool:
    return any(
        message.message_type == "agent_patch_rejected"
        and message.card.get("reason") == "target_mismatch_new_work"
        for message in result.outbound_messages
    )

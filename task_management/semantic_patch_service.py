from __future__ import annotations

from .relations import (
    ATTENDEES_KEY,
    COMPLETED_AT_KEY,
    DATE_WINDOW_END_KEY,
    DATE_WINDOW_LABEL_KEY,
    DATE_WINDOW_START_KEY,
    DEFERRED_MISSING_SLOTS_KEY,
    DEFERRED_REASON_KEY,
    DEFERRED_REMINDER_CADENCE_HOURS_KEY,
    DEFERRED_UNTIL_KEY,
    EXTERNAL_PARTICIPANTS_KEY,
    LAST_SEMANTIC_PATCH_ACTOR_ID_KEY,
    LAST_SEMANTIC_PATCH_AT_KEY,
    LAST_SEMANTIC_PATCH_CONFIDENCE_KEY,
    LAST_SEMANTIC_PATCH_EVIDENCE_KEY,
    LAST_STATE_LINKED_UPDATE_TYPE_KEY,
    LOCATION_KEY,
    LOCATION_OPTIONAL_KEY,
    NEEDS_EXACT_TIME_KEY,
    NEEDS_PREP_KEY,
    PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
    PROGRESS_NOTE_KEY,
    PROGRESS_PERCENT_KEY,
    PROGRESS_STATUS_KEY,
    PROGRESS_UPDATED_AT_KEY,
    REMAINING_WORK_KEY,
)

from dataclasses import replace
from datetime import datetime
import re
from typing import Callable

from .approval_flow import record_decision
from .completion_linker import related_commitments
from .conflict_policy import recompute_missing_slots
from .deferred_policy import csv_dedupe, default_deferred_until
from .domain import (
    ApprovalRequest,
    OrchestrationResult,
    OutboundMessage,
    Proposal,
    ProposalKind,
)
from .approval_policy import missing_slot_question_message
from .operating_agent import ProposalPatch
from .pending_info import missing_info_followup_message
from .slot_validator import missing_slots_for_proposal
from .store import TeamTaskStore
from .update_messages import (
    _patch_rejection_message,
    _semantic_direct_update_message,
)


MIN_SEMANTIC_TARGET_CONFIDENCE = 0.65
_SEMANTIC_CORRECTION_KEYS = (
    "title",
    "corrected_title",
    "due_date",
    "scheduled_date",
    DATE_WINDOW_START_KEY,
    "time_window",
    PARTICIPANTS_KEY,
    "external_owner",
    EXTERNAL_PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
    ATTENDEES_KEY,
    LOCATION_KEY,
    LOCATION_OPTIONAL_KEY,
    "materials",
    NEEDS_PREP_KEY,
    NEEDS_EXACT_TIME_KEY,
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


class SemanticPatchService:
    """Gating + application of agent-produced semantic proposal patches.

    Owns the MIN_SEMANTIC_TARGET_CONFIDENCE gate, the rejection-reason ordering,
    and the direct-patch appliers. Orchestrator-owned behaviours are injected as
    callables so this service stays free of an import cycle with the orchestrator.
    """

    def __init__(
        self,
        store: TeamTaskStore,
        *,
        route_approval: Callable[..., OrchestrationResult],
        route_feedback: Callable[..., OrchestrationResult],
        ensure_missing_slot_request: Callable[..., tuple[ApprovalRequest, bool]],
    ) -> None:
        self.store = store
        self._route_approval = route_approval
        self._route_feedback = route_feedback
        self._ensure_missing_slot_request = ensure_missing_slot_request

    def apply_patches(
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
            if patch.request_id and _is_approval_rejection_update(patch.temporal_update):
                result = self._route_approval(
                    request_id=patch.request_id,
                    approver_id=actor_id,
                    accepted=False,
                    decided_at=changed_at,
                )
            elif patch.request_id and _is_completion_update(patch.temporal_update):
                result = self._handle_request_completion_patch(
                    patch,
                    actor_id=actor_id,
                    changed_at=changed_at,
                )
            elif patch.request_id:
                result = self._route_feedback(
                    request_id=patch.request_id,
                    actor_id=actor_id,
                    body=patch.body,
                    changed_at=changed_at,
                    temporal_update=dict(patch.temporal_update),
                    expected_proposal_id=patch.proposal_id,
                )
            else:
                result = self._handle_direct_semantic_patch(
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
        is_request_rejection = bool(patch.request_id and _is_approval_rejection_update(patch.temporal_update))
        if not is_request_rejection:
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
            if _is_completion_update(patch.temporal_update):
                if proposal.status == "rejected":
                    return "target_not_completable"
                return ""
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
        proposal = self.store.get_proposal(request.proposal_id)
        if proposal is not None and _is_completion_update(patch.temporal_update) and proposal.status == "rejected":
            return "target_not_completable"
        if proposal is not None and _looks_like_unrelated_new_work_patch(patch, proposal):
            return "target_mismatch_new_work"
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
        if patch.temporal_update.get(PROGRESS_STATUS_KEY):
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
            LAST_SEMANTIC_PATCH_ACTOR_ID_KEY: actor_id,
            LAST_SEMANTIC_PATCH_AT_KEY: changed_at.isoformat(timespec="seconds"),
            LAST_SEMANTIC_PATCH_CONFIDENCE_KEY: f"{patch.target_confidence:.2f}",
            LAST_SEMANTIC_PATCH_EVIDENCE_KEY: patch.evidence_text,
            LAST_STATE_LINKED_UPDATE_TYPE_KEY: linked_type,
        }
        missing_slots = recompute_missing_slots(replace(changed, metadata=metadata))
        status = changed.status
        required = changed.required_approvers or (actor_id,)
        approvals = changed.approvals or ((actor_id,) if changed.assigned_to == actor_id else ())
        deferred_only = _missing_slots_all_deferred(metadata, missing_slots, now=changed_at)
        if missing_slots and not deferred_only:
            status = "awaiting_approval"
            approvals = tuple(item for item in approvals if item in required)
        elif not missing_slots and status in {"draft", "awaiting_approval"} and set(required).issubset(set((*approvals, actor_id))):
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
        if missing_slots and deferred_only:
            return OrchestrationResult(
                proposals=(updated,),
                outbound_messages=(
                    OutboundMessage(
                        surface="personal_chat",
                        recipient_id=actor_id,
                        message_type="missing_info_deferred",
                        text=(
                            f"알겠습니다. {updated.title}은(는) 아직 정해지지 않은 정보가 있는 상태로 두고, "
                            "필요한 시점에 다시 확인하겠습니다."
                        ),
                        proposal_id=updated.proposal_id,
                        card={
                            "proposal_id": updated.proposal_id,
                            "missing_slots": ", ".join(updated.missing_slots),
                            DEFERRED_UNTIL_KEY: updated.metadata.get(DEFERRED_UNTIL_KEY, ""),
                        },
                    ),
                ),
            )
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
            COMPLETED_AT_KEY: changed_at.isoformat(timespec="seconds"),
            "completion_source": "semantic_patch",
            LAST_SEMANTIC_PATCH_ACTOR_ID_KEY: actor_id,
            LAST_SEMANTIC_PATCH_AT_KEY: changed_at.isoformat(timespec="seconds"),
            LAST_SEMANTIC_PATCH_CONFIDENCE_KEY: f"{patch.target_confidence:.2f}",
            LAST_SEMANTIC_PATCH_EVIDENCE_KEY: patch.evidence_text,
            LAST_STATE_LINKED_UPDATE_TYPE_KEY: "semantic_completion",
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
        related: list[Proposal] = []
        for candidate in related_commitments(self.store.list_proposals(), completed, patch):
            metadata = {
                **candidate.metadata,
                "completed_by": actor_id,
                COMPLETED_AT_KEY: changed_at.isoformat(timespec="seconds"),
                "completion_source": "semantic_linked_completion",
                "linked_completion_source_proposal_id": completed.proposal_id,
                LAST_SEMANTIC_PATCH_ACTOR_ID_KEY: actor_id,
                LAST_SEMANTIC_PATCH_AT_KEY: changed_at.isoformat(timespec="seconds"),
                LAST_SEMANTIC_PATCH_CONFIDENCE_KEY: f"{patch.target_confidence:.2f}",
                LAST_SEMANTIC_PATCH_EVIDENCE_KEY: patch.evidence_text,
                LAST_STATE_LINKED_UPDATE_TYPE_KEY: "semantic_linked_completion",
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
            PROGRESS_STATUS_KEY: patch.temporal_update.get(PROGRESS_STATUS_KEY, "partial"),
            PROGRESS_NOTE_KEY: patch.body.strip(),
            PROGRESS_UPDATED_AT_KEY: changed_at.isoformat(timespec="seconds"),
            "progress_updated_by": actor_id,
            LAST_SEMANTIC_PATCH_ACTOR_ID_KEY: actor_id,
            LAST_SEMANTIC_PATCH_AT_KEY: changed_at.isoformat(timespec="seconds"),
            LAST_SEMANTIC_PATCH_CONFIDENCE_KEY: f"{patch.target_confidence:.2f}",
            LAST_SEMANTIC_PATCH_EVIDENCE_KEY: patch.evidence_text,
            LAST_STATE_LINKED_UPDATE_TYPE_KEY: linked_type,
        }
        if patch.temporal_update.get(PROGRESS_PERCENT_KEY):
            metadata[PROGRESS_PERCENT_KEY] = patch.temporal_update[PROGRESS_PERCENT_KEY]
        if patch.temporal_update.get(REMAINING_WORK_KEY):
            metadata[REMAINING_WORK_KEY] = patch.temporal_update[REMAINING_WORK_KEY]
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

    def _handle_request_completion_patch(
        self,
        patch: ProposalPatch,
        *,
        actor_id: str,
        changed_at: datetime,
    ) -> OrchestrationResult:
        request = self.store.get_approval_request(patch.request_id)
        if request is None:
            return OrchestrationResult()
        proposal = self.store.get_proposal(request.proposal_id)
        if proposal is None:
            return OrchestrationResult()
        decided_request, _decision = record_decision(
            self.store,
            request,
            approver_id=actor_id,
            accepted=True,
            decided_at=changed_at,
        )
        completion_patch = replace(patch, proposal_id=proposal.proposal_id)
        result = self._handle_direct_completion_patch(
            completion_patch,
            actor_id=actor_id,
            changed_at=changed_at,
            proposal=proposal,
        )
        return OrchestrationResult(
            proposals=result.proposals,
            approval_requests=(decided_request,),
            outbound_messages=result.outbound_messages,
        )


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
    resolves_date_window = any(temporal.get(key) for key in ("due_date", "scheduled_date", DATE_WINDOW_START_KEY))
    if resolves_date_window:
        for key in (DATE_WINDOW_START_KEY, DATE_WINDOW_END_KEY, DATE_WINDOW_LABEL_KEY, "needs_exact_date"):
            if key in temporal:
                continue
            metadata.pop(key, None)
        if proposal.metadata.get(DATE_WINDOW_START_KEY) and proposal.metadata.get(DATE_WINDOW_END_KEY):
            metadata["resolved_date_window_start"] = proposal.metadata[DATE_WINDOW_START_KEY]
            metadata["resolved_date_window_end"] = proposal.metadata[DATE_WINDOW_END_KEY]
            metadata["resolved_date_window_label"] = proposal.metadata.get(DATE_WINDOW_LABEL_KEY, "")
    if temporal.get("due_date"):
        due_date = datetime.fromisoformat(temporal["due_date"]).date()
        scheduled_date = None
    if temporal.get("scheduled_date"):
        scheduled_date = datetime.fromisoformat(temporal["scheduled_date"]).date()
        due_date = None
    if temporal.get(DATE_WINDOW_START_KEY):
        metadata.update(
            {
                DATE_WINDOW_START_KEY: temporal[DATE_WINDOW_START_KEY],
                DATE_WINDOW_END_KEY: temporal.get(DATE_WINDOW_END_KEY, ""),
                DATE_WINDOW_LABEL_KEY: temporal.get(DATE_WINDOW_LABEL_KEY, ""),
                "needs_exact_date": temporal.get("needs_exact_date", "true"),
            }
        )
    for key in (
        PARTICIPANTS_KEY,
        "external_owner",
        EXTERNAL_PARTICIPANTS_KEY,
        PARTICIPANT_LABEL_KEY,
        ATTENDEES_KEY,
        LOCATION_KEY,
        LOCATION_OPTIONAL_KEY,
        "materials",
        NEEDS_PREP_KEY,
        NEEDS_EXACT_TIME_KEY,
    ):
        if temporal.get(key):
            metadata[key] = temporal[key]
    if temporal.get("defer_missing_slots"):
        deferred_slots = csv_dedupe(temporal["defer_missing_slots"])
        if deferred_slots:
            metadata[DEFERRED_MISSING_SLOTS_KEY] = ",".join(deferred_slots)
            metadata[DEFERRED_REASON_KEY] = "not_decided"
            metadata["deferred_at"] = changed_at.isoformat(timespec="seconds")
            metadata[DEFERRED_UNTIL_KEY] = temporal.get(DEFERRED_UNTIL_KEY) or default_deferred_until(
                scheduled_date or due_date,
                changed_at=changed_at,
            )
            metadata[DEFERRED_REMINDER_CADENCE_HOURS_KEY] = temporal.get(DEFERRED_REMINDER_CADENCE_HOURS_KEY, "2")

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
            DATE_WINDOW_START_KEY,
            "time_window",
            PARTICIPANTS_KEY,
            "external_owner",
            EXTERNAL_PARTICIPANTS_KEY,
            PARTICIPANT_LABEL_KEY,
            ATTENDEES_KEY,
            LOCATION_KEY,
            LOCATION_OPTIONAL_KEY,
            "materials",
            NEEDS_PREP_KEY,
            NEEDS_EXACT_TIME_KEY,
            "defer_missing_slots",
            "status",
            PROGRESS_STATUS_KEY,
            PROGRESS_PERCENT_KEY,
            REMAINING_WORK_KEY,
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
            DATE_WINDOW_START_KEY,
            "time_window",
            PARTICIPANTS_KEY,
            "external_owner",
            EXTERNAL_PARTICIPANTS_KEY,
            PARTICIPANT_LABEL_KEY,
            ATTENDEES_KEY,
            LOCATION_KEY,
            LOCATION_OPTIONAL_KEY,
            "materials",
            NEEDS_PREP_KEY,
            NEEDS_EXACT_TIME_KEY,
            "defer_missing_slots",
            "title",
            "corrected_title",
        )
    )


def _is_scoped_progress_completion(update: dict[str, str]) -> bool:
    scope = update.get("completion_scope", "").strip().lower()
    return (
        update.get("semantic_update_type") == "completion"
        and update.get("status") == "done"
        and bool(update.get(PROGRESS_STATUS_KEY))
        and scope in _SCOPED_COMPLETION_SCOPES
    )


def _progress_update_from_scoped_completion(update: dict[str, str]) -> dict[str, str]:
    adjusted = dict(update)
    adjusted.pop("status", None)
    adjusted["semantic_update_type"] = "progress"
    adjusted[PROGRESS_STATUS_KEY] = adjusted.get(PROGRESS_STATUS_KEY) or "complete"
    return adjusted


def _is_confirmation_update(update: dict[str, str]) -> bool:
    return update.get("semantic_update_type") == "confirmation" or update.get("status") == "confirmed"


def _is_approval_rejection_update(update: dict[str, str]) -> bool:
    status = update.get("status", "").strip().lower()
    update_type = update.get("semantic_update_type", "").strip().lower()
    return status in {"rejected", "reject"} or update_type in {"rejection", "approval_rejection"}


def _is_completion_update(update: dict[str, str]) -> bool:
    return update.get("status") == "done" or update.get("semantic_update_type") == "completion"


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
    if update_type == "progress" and not update.get(PROGRESS_STATUS_KEY):
        return "progress_requires_progress_status"
    if update_type == "deferral" and not any(update.get(key) for key in ("due_date", "scheduled_date", "time_window")):
        return "deferral_requires_temporal_update"
    if update_type == "confirmation" and not (
        status == "confirmed" or any(update.get(key) for key in ("due_date", "scheduled_date", "time_window", LOCATION_KEY))
    ):
        return "confirmation_requires_confirmed_status_or_slot"
    return ""


def _looks_like_unrelated_new_work_patch(patch: ProposalPatch, proposal: Proposal) -> bool:
    if patch.target_confidence >= 0.85:
        return False
    text = f"{patch.body} {patch.evidence_text}".strip()
    if not text:
        return False
    if _has_patch_target_evidence(text, patch, proposal):
        return False
    update = patch.temporal_update
    if not any(update.get(key) for key in ("due_date", "scheduled_date", "time_window", LOCATION_KEY)):
        return False
    compact = text.replace(" ", "").lower()
    new_work_tokens = (
        "회의",
        "미팅",
        "면담",
        "방문",
        "예약",
        "후속",
        "연락",
        "진행",
        "해야",
        "할일",
        "할 일",
    )
    return any(token.replace(" ", "") in compact for token in new_work_tokens)


def _has_patch_target_evidence(text: str, patch: ProposalPatch, proposal: Proposal) -> bool:
    normalized = text.replace(" ", "").lower()
    if patch.request_id and patch.request_id.lower() in normalized:
        return True
    if patch.proposal_id and patch.proposal_id.lower() in normalized:
        return True
    title = proposal.title.replace(" ", "").lower()
    if title and title in normalized:
        return True
    if any(token in normalized for token in ("방금", "앞서", "아까", "해당", "그건", "그거", "그일정", "이항목", "그항목")):
        return True
    title_tokens = _semantic_target_tokens(proposal.title)
    text_tokens = _semantic_target_tokens(text)
    return len(title_tokens & text_tokens) >= 2


def _semantic_target_tokens(text: str) -> set[str]:
    generic = {
        "회의",
        "미팅",
        "논의",
        "일정",
        "작업",
        "업무",
        "자료",
        "준비",
        "확인",
        "요청",
        "후속",
        "진행",
        "장소",
        "시간",
        "날짜",
    }
    tokens = {token for token in re.split(r"[^0-9A-Za-z가-힣]+", text.lower()) if len(token) >= 2}
    return {token for token in tokens if token not in generic}


def _missing_slots_all_deferred(
    metadata: dict[str, str],
    missing_slots: tuple[str, ...],
    *,
    now: datetime,
) -> bool:
    """True when every still-missing slot was explicitly deferred to a future time.

    Used by the direct semantic-patch path to acknowledge a deferral instead of
    demoting an already-approved proposal and re-asking the deferred slot.
    """

    if not missing_slots:
        return False
    deferred = {
        item.strip()
        for item in metadata.get(DEFERRED_MISSING_SLOTS_KEY, "").split(",")
        if item.strip()
    }
    if not deferred or not set(missing_slots).issubset(deferred):
        return False
    raw = metadata.get(DEFERRED_UNTIL_KEY, "")
    if not raw:
        return False
    try:
        deferred_until = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return deferred_until > now

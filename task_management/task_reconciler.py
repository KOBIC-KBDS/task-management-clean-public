from __future__ import annotations

from .relations import (
    ATTENDEES_KEY,
    CONFLICT_DETECTED_KEY,
    CONFLICT_WITH_PROPOSAL_IDS_KEY,
    DEFERRED_MISSING_SLOTS_KEY,
    DEFERRED_REASON_KEY,
    DEFERRED_REMINDER_CADENCE_HOURS_KEY,
    DEFERRED_UNTIL_KEY,
    EXTERNAL_PARTICIPANTS_KEY,
    LAST_STATE_LINKED_UPDATE_TYPE_KEY,
    LINK_PREP_SUBTASK,
    LINK_TYPE_KEY,
    LOCATION_KEY,
    LOCATION_OPTIONAL_KEY,
    NEEDS_EXACT_TIME_KEY,
    NEEDS_PREP_KEY,
    PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
)

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Callable, Literal
import hashlib
import os
import re

from .channels import channel_for_message_id
from .conflict_policy import apply_conflict_resolution_feedback, recompute_missing_slots
from .deferred_policy import csv_dedupe, default_deferred_until
from .domain import ApprovalDecision, IncomingMessage, OrchestrationResult, Proposal
from .discussion_adapter import parse_temporal_update
from .korean_time import relative_date
from .feedback_scoring import (
    CONTEXT_TOKEN_WEIGHT,
    DEICTIC_CONFLICT_BONUS,
    DISCUSSION_WORD_BONUS,
    NEXT_WEEK_BONUS,
    RAW_TOKEN_WEIGHT,
    RETURN_BONUS,
    SLOT_HINT_BONUS,
    TITLE_TOKEN_WEIGHT,
    pick_best_candidate,
)
from .operating_agent import OperatingAgentDecision
from .source_refs import source_metadata as _source_metadata, text_hash as _text_hash
from .store import TeamTaskStore


@dataclass(frozen=True)
class ReconcilerRule:
    """One deterministic reconciliation scenario in declarative form.

    A rule is the registry-shaped contract behind the two historical reconciler
    tails.  ``match`` does the cheap detection (and any parent/parse lookup),
    returning an opaque match-object the rule's ``apply`` consumes, or ``None``
    when the rule does not fire.  ``apply`` performs the persistence-bearing
    mutation and returns the produced proposals.  Rules share the existing
    persistence helpers (``_persist_reconciled_change`` and the
    ``proposal.created``/``proposal.approved`` emission) so a new scenario is a
    single ``ReconcilerRule`` entry plus its ``match``/``apply`` functions.

    INVARIANT 1 (reconciler is fallback-only) and INVARIANT 8 (the score/gap
    gates) are enforced by the orchestrator and the central branch chain in
    ``apply_state_linked_update`` *before* any ``update_existing`` rule runs;
    rules never relax those gates.
    """

    name: str
    phase: Literal["create_followup", "update_existing"]
    priority: int
    match: Callable[[TeamTaskStore, IncomingMessage], object | None]
    apply: Callable[[TeamTaskStore, object, datetime], tuple[Proposal, ...]]


def _rules_for_phase(phase: str) -> tuple[ReconcilerRule, ...]:
    return tuple(
        rule
        for rule in sorted(RECONCILER_RULES, key=lambda item: item.priority)
        if rule.phase == phase
    )


def reconcile_message(
    store: TeamTaskStore,
    message: IncomingMessage,
    result: OrchestrationResult | None = None,
    *,
    reconciled_at: datetime | None = None,
) -> tuple[Proposal, ...]:
    """Create deterministic follow-up proposals that the semantic agent may miss.

    The operating agent remains the primary semantic extractor.  This reconciler is
    deliberately narrow: it only promotes relationship-shaped updates that require
    existing state, such as "make the presentation materials in the morning" after
    an already-approved event was captured.
    """

    del result  # reserved for future reconciliation between agent output and store state
    now = reconciled_at or message.received_at
    created: list[Proposal] = []
    for rule in _rules_for_phase("create_followup"):
        matched = rule.match(store, message)
        if matched is None:
            continue
        created.extend(rule.apply(store, matched, now))
    if created:
        store.append_event(
            "slack.message.reconciled",
            {
                "message_id": message.message_id,
                "proposal_ids": [proposal.proposal_id for proposal in created],
            },
            occurred_at=now,
        )
    return tuple(created)


def _is_slack_notification_candidate_message(message: IncomingMessage) -> bool:
    channel = channel_for_message_id(message.message_id)
    return (
        message.visibility == "team"
        and channel is not None
        and channel.notification_fallback_candidate
    )


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


def apply_state_linked_update(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    updated_at: datetime | None = None,
) -> tuple[Proposal, ...]:
    """Apply deterministic updates to existing proposals as a fallback after the agent.

    This runs only after the operating agent returns ``no_action`` from a non-trusted
    source (see ``_should_run_state_linked_fallback``); the agent remains
    the primary semantic extractor.  When multiple proposals are pending, split the
    answer into meaning-bearing clauses, resolve each clause to a concrete proposal, and
    only then apply slots.  If a clause cannot be mapped confidently, do not mutate an
    arbitrary "first" pending item.
    """

    now = updated_at or message.received_at
    updated: list[Proposal] = []
    conflict_resolution = apply_conflict_resolution_feedback(store, message, now=now)
    if conflict_resolution is not None:
        updated.append(conflict_resolution)
    else:
        semantic_updates = _semantic_pending_feedback_updates(store, message, now=now)
        if semantic_updates:
            updated.extend(semantic_updates)
        elif _looks_like_multi_pending_feedback(store, message):
            store.append_event(
                "state_linked_update.ambiguous",
                {
                    "message_id": message.message_id,
                    "reason": "multi_pending_feedback_without_confident_target",
                    "text_hash": _text_hash(message.text),
                },
                occurred_at=now,
            )
        else:
            deferred_missing_info = _deferred_missing_info_update(store, message, now=now)
            if deferred_missing_info is not None:
                updated.append(deferred_missing_info)
            else:
                resolved_missing_info = _missing_info_resolution_update(store, message, now=now)
                if resolved_missing_info is not None:
                    updated.append(resolved_missing_info)
            for rule in _rules_for_phase("update_existing"):
                matched = rule.match(store, message)
                if matched is None:
                    continue
                updated.extend(rule.apply(store, matched, now))
    if updated:
        store.append_event(
            "slack.message.reconciled",
            {
                "message_id": message.message_id,
                "proposal_ids": [proposal.proposal_id for proposal in updated],
                "change_type": "state_linked_update",
            },
            occurred_at=now,
        )
    return tuple(updated)


def _semantic_pending_feedback_updates(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> tuple[Proposal, ...]:
    """Resolve multi-clause feedback against pending proposals before slot parse.

    Runs only after the operating agent returns ``no_action`` from a non-trusted
    source (see ``_should_run_state_linked_fallback``).  It keeps the
    same invariant the agent must keep: propose targeted patches with evidence,
    never spray one parsed date/time across all pending tasks.
    """

    candidates = _pending_info_candidates(store)
    if len(candidates) < 2:
        return ()

    clauses = _semantic_feedback_clauses(message.text)
    planned: list[tuple[str, dict[str, str], str, Proposal]] = []
    used_ids: set[str] = set()
    has_unmatched_actionable_clause = False
    for clause in clauses:
        if not _has_pending_feedback_target_evidence(candidates, clause):
            if _looks_like_new_work_clause(clause, reference_date=message.received_at.date()):
                has_unmatched_actionable_clause = True
            continue
        temporal = parse_temporal_update(clause, reference_date=message.received_at.date())
        assigned_to = _assignee_from_feedback_text(clause, sender_id=message.sender_id)
        if not (_has_resolution_signal(temporal) or assigned_to):
            if _looks_like_new_work_clause(clause, reference_date=message.received_at.date()):
                has_unmatched_actionable_clause = True
            continue
        proposal = _resolve_feedback_clause_target(candidates, clause, used_ids=used_ids)
        if proposal is None:
            if _looks_like_new_work_clause(clause, reference_date=message.received_at.date()):
                has_unmatched_actionable_clause = True
            continue
        planned.append((clause, temporal, assigned_to, proposal))
        used_ids.add(proposal.proposal_id)

    if planned and has_unmatched_actionable_clause:
        store.append_event(
            "state_linked_update.deferred_to_agent",
            {
                "message_id": message.message_id,
                "reason": "mixed_targeted_feedback_and_new_work",
                "matched_proposal_ids": [proposal.proposal_id for _, _, _, proposal in planned],
                "clause_count": len(clauses),
            },
            occurred_at=now,
        )
        return ()

    updated: list[Proposal] = []
    for clause, temporal, assigned_to, proposal in planned:
        changed = _apply_semantic_clause_update(
            store,
            proposal,
            clause,
            temporal,
            assigned_to=assigned_to,
            actor_id=message.sender_id,
            message_id=message.message_id,
            now=now,
        )
        if changed is None:
            continue
        updated.append(changed)
    return tuple(updated)


def _pending_info_candidates(store: TeamTaskStore) -> tuple[Proposal, ...]:
    return tuple(
        proposal
        for proposal in store.list_proposals()
        if proposal.status == "awaiting_approval" and proposal.missing_slots
    )


def _semantic_feedback_clauses(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"[\r\n]+", ". ", text.strip())
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return ()
    normalized = re.sub(
        r"\s+(?=(?:차주|다음 주|다음주|복귀해서는|그리고|또|추가로|추가적으로)\b)",
        ". ",
        normalized,
    )
    parts = re.split(r"(?<=[.!?。])\s+|[;\n]+", normalized)
    return tuple(part.strip(" .!?。") for part in parts if part.strip(" .!?。"))


def _has_pending_feedback_target_evidence(candidates: tuple[Proposal, ...], clause: str) -> bool:
    """Return true only when a clause looks like feedback for existing state."""

    normalized = clause.replace(" ", "").lower()
    if "approval/" in normalized or "proposal/" in normalized:
        return True
    if _is_this_discussion_clause(normalized) or any(
        token in normalized for token in ("방금", "앞에서", "위일정", "그일정", "해당일정", "그항목", "이항목")
    ):
        return True

    return any(_shares_pending_info_context(proposal, clause) for proposal in candidates)


def _resolve_feedback_clause_target(
    candidates: tuple[Proposal, ...],
    clause: str,
    *,
    used_ids: set[str],
) -> Proposal | None:
    scored = [
        (score, proposal)
        for proposal in candidates
        if proposal.proposal_id not in used_ids
        for score in (_feedback_clause_score(proposal, clause),)
        if score > 0
    ]
    return pick_best_candidate(scored)


def _feedback_clause_score(proposal: Proposal, clause: str) -> int:
    normalized = clause.lower()
    score = 0
    if _is_this_discussion_clause(normalized):
        if proposal.metadata.get(CONFLICT_DETECTED_KEY) == "true" or "conflict_resolution" in proposal.missing_slots:
            score += DEICTIC_CONFLICT_BONUS
        if any(token in f"{proposal.title} {proposal.raw_text}".lower() for token in ("논의", "할일", "할 일", "정리")):
            score += DISCUSSION_WORD_BONUS
    title_tokens = _clean_context_tokens(proposal.title)
    raw_tokens = _clean_context_tokens(proposal.raw_text)
    clause_tokens = _clean_context_tokens(clause)
    title_overlap = _strong_target_tokens(title_tokens & clause_tokens)
    raw_overlap = _strong_target_tokens((raw_tokens - title_tokens) & clause_tokens)
    context_overlap = _strong_target_tokens(_target_context_tokens(proposal) & clause_tokens)
    score += TITLE_TOKEN_WEIGHT * len(title_overlap)
    score += RAW_TOKEN_WEIGHT * len(raw_overlap)
    score += CONTEXT_TOKEN_WEIGHT * len(context_overlap)
    if "차주" in normalized and any(token in f"{proposal.title} {proposal.raw_text}" for token in ("차주", "복귀")):
        score += NEXT_WEEK_BONUS
    if "복귀" in normalized and "복귀" in f"{proposal.title} {proposal.raw_text}":
        score += RETURN_BONUS
    if "담당" in normalized and "assigned_to" in proposal.missing_slots:
        score += SLOT_HINT_BONUS
    if any(token in normalized for token in ("일시", "언제", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")) and any(
        slot in proposal.missing_slots for slot in ("date", "time", "exact_date")
    ):
        score += SLOT_HINT_BONUS
    return score


def _looks_like_new_work_clause(clause: str, *, reference_date: date) -> bool:
    """Identify actionable new-work clauses so the semantic agent can own them."""

    compact = clause.replace(" ", "").lower()
    if not compact:
        return False
    if _has_explicit_target_id(clause) or _has_deictic_pointer(clause):
        return False

    temporal = parse_temporal_update(clause, reference_date=reference_date)
    deadline_signal = bool({"due_date", "scheduled_date", "time_window"}.intersection(temporal)) or any(
        token in compact
        for token in (
            "까지",
            "퇴근전",
            "오전까지",
            "오후까지",
            "오늘안",
            "내일",
            "이번주",
            "다음주",
            "차주",
        )
    )
    owner_signal = any(
        token in compact
        for token in (
            "내가",
            "제가",
            "나는",
            "저는",
            "나한테",
            "나에게",
            "우리",
        )
    )
    action_signal = any(
        token in compact
        for token in (
            "해야",
            "해야함",
            "해야하는",
            "해야할",
            "하자",
            "할게",
            "완성",
            "전달",
            "공유",
            "보내",
            "준비",
            "작성",
            "정리",
            "반영",
            "검토",
            "확인",
            "진행",
            "등록",
            "추적",
            "추가",
        )
    )
    return action_signal and (owner_signal or deadline_signal)


def _target_context_tokens(proposal: Proposal) -> set[str]:
    metadata_text = " ".join(
        value
        for key, value in proposal.metadata.items()
        if key
        in {
            "materials",
            "parent_title",
            "external_owner",
            EXTERNAL_PARTICIPANTS_KEY,
            PARTICIPANT_LABEL_KEY,
        }
    )
    return _clean_context_tokens(metadata_text)


def _is_this_discussion_clause(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(token in compact for token in ("이논의", "이건", "이것", "그논의", "그건", "그것"))


def _assignee_from_feedback_text(text: str, *, sender_id: str) -> str:
    compact = text.replace(" ", "").lower()
    if any(token in compact for token in _self_assignee_tokens()):
        return sender_id if sender_id in {"me", "teammate"} else "me"
    if any(token in compact for token in ("팀원가담당", "팀원가담당", "남편이담당", "아내가담당", "teammate")):
        return "teammate"
    if any(token in compact for token in ("같이담당", "공동담당", "우리담당", "shared")):
        return "shared"
    return ""


def _self_assignee_tokens() -> tuple[str, ...]:
    tokens = ["내가담당", "내담당", "담당자는나", "담당은나", "제가담당"]
    configured_label = os.environ.get("TASK_MANAGEMENT_ACTOR_LABEL_ME", "")
    if configured_label:
        tokens.append(configured_label.replace(" ", "").lower())
    return tuple(tokens)


def _derive_schedule_fields(
    proposal: Proposal,
    temporal: dict[str, str],
    *,
    prefers_due_task: bool = False,
) -> tuple[date | None, date | None, str]:
    """Derive (due_date, scheduled_date, kind) from a temporal update.

    Owns the due/scheduled mutual exclusion (a temporal due_date clears scheduled_date and
    vice versa) and the base kind reclassification shared by both reconciled-update tails:
    a scheduled_date makes the item an event; a due_date on a pending question makes it a
    task.  When ``prefers_due_task`` is set, a scheduled_date is reinterpreted as a due_date
    task (the semantic path's "할일" interpretation).
    """

    due_date = proposal.due_date
    scheduled_date = proposal.scheduled_date
    if temporal.get("due_date"):
        due_date = datetime.fromisoformat(temporal["due_date"]).date()
        scheduled_date = None
    if temporal.get("scheduled_date"):
        scheduled_date = datetime.fromisoformat(temporal["scheduled_date"]).date()
        due_date = None
    if prefers_due_task and scheduled_date is not None:
        due_date = scheduled_date
        scheduled_date = None

    kind = proposal.kind
    if prefers_due_task:
        kind = "task"
    elif scheduled_date is not None:
        kind = "event"
    elif due_date is not None and kind == "question":
        kind = "task"
    return due_date, scheduled_date, kind


def _persist_reconciled_change(
    store: TeamTaskStore,
    original: Proposal,
    changed: Proposal,
    *,
    change_body: str,
    actor_id: str,
    change_type: str,
    now: datetime,
) -> Proposal | None:
    """Recompute slots, settle approval, persist, and emit the proposal events.

    Shared tail for both reconciled-update paths: recompute missing slots, mark the
    missing-info request approved when nothing is left open, skip persistence when the
    recompute produced no change, then save and emit ``proposal.changed`` (and
    ``proposal.approved`` once the proposal reaches ``approved``).
    """

    changed = replace(changed, missing_slots=recompute_missing_slots(changed), updated_at=now)
    if not changed.missing_slots:
        changed = _mark_missing_info_approved(store, changed, actor_id=actor_id, now=now)
    elif changed == original:
        return None

    store.save_proposal(changed)
    store.append_event(
        "proposal.changed",
        {
            "proposal": changed,
            "change_body": change_body,
            "actor_id": actor_id,
            "reconciled": True,
            "change_type": change_type,
        },
        occurred_at=now,
    )
    if changed.status == "approved":
        store.append_event("proposal.approved", {"proposal": changed, "reconciled": True}, occurred_at=now)
    return changed


def _apply_semantic_clause_update(
    store: TeamTaskStore,
    proposal: Proposal,
    clause: str,
    temporal: dict[str, str],
    *,
    assigned_to: str,
    actor_id: str,
    message_id: str,
    now: datetime,
) -> Proposal | None:
    metadata = _apply_temporal_metadata(
        proposal.metadata,
        temporal,
        actor_id=actor_id,
        message_id=message_id,
        now=now,
    )
    metadata.update(
        {
            "state_linked_resolver": "semantic_pending_feedback.v1",
            "state_linked_clause": clause,
        }
    )

    due_date, scheduled_date, kind = _derive_schedule_fields(proposal, temporal)
    prefers_due_task = scheduled_date is not None and _semantic_clause_prefers_due_task(proposal, clause)
    if prefers_due_task:
        due_date, scheduled_date, kind = _derive_schedule_fields(
            proposal, temporal, prefers_due_task=True
        )
        metadata["semantic_schedule_interpretation"] = "due_task"
    if not temporal.get(LOCATION_KEY) and _looks_like_stale_location(metadata.get(LOCATION_KEY, "")):
        metadata.pop(LOCATION_KEY, None)

    missing_slots = proposal.missing_slots
    if _semantic_clause_reclassifies_conflict_as_task(proposal, clause, temporal):
        kind = "task"
        scheduled_date = None
        metadata["semantic_correction"] = "not_blocking_event_due_task"
        metadata["previous_conflict_detected"] = metadata.pop(CONFLICT_DETECTED_KEY, "")
        metadata.pop(CONFLICT_WITH_PROPOSAL_IDS_KEY, None)
        metadata.pop("conflict_policy", None)
        metadata.pop("blocks_in_person", None)
        metadata.pop("event_scope", None)
        missing_slots = tuple(slot for slot in missing_slots if slot != "conflict_resolution")

    changed = replace(
        proposal,
        kind=kind,
        assigned_to=assigned_to or proposal.assigned_to,
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=temporal.get("time_window", proposal.time_window),
        missing_slots=missing_slots,
        metadata=metadata,
        updated_at=now,
    )
    return _persist_reconciled_change(
        store,
        proposal,
        changed,
        change_body=clause,
        actor_id=actor_id,
        change_type="semantic_pending_feedback",
        now=now,
    )


def _semantic_clause_reclassifies_conflict_as_task(
    proposal: Proposal,
    clause: str,
    temporal: dict[str, str],
) -> bool:
    if proposal.metadata.get(CONFLICT_DETECTED_KEY) != "true" and "conflict_resolution" not in proposal.missing_slots:
        return False
    if not temporal.get("due_date"):
        return False
    compact = clause.replace(" ", "")
    return any(token in compact for token in ("할일", "할일이야", "해야", "전에", "까지", "퇴근"))


def _semantic_clause_prefers_due_task(proposal: Proposal, clause: str) -> bool:
    haystack = f"{proposal.title} {proposal.raw_text} {clause}"
    if any(token in haystack for token in ("회의", "미팅", "회식", "참석", "예약", "데려", "방문")):
        return False
    return any(token in haystack for token in ("할일", "할 일", "정리", "확인", "준비", "처리", "작성"))


def _looks_like_stale_location(value: str) -> bool:
    normalized = value.strip(" )]}.，,。").replace(" ", "")
    if not normalized:
        return False
    return normalized.startswith(("전", "전에", "까지", "경", "쯤")) or any(
        token in normalized for token in ("할일", "일시", "담당", "좋겠", "이야")
    )


def _looks_like_multi_pending_feedback(store: TeamTaskStore, message: IncomingMessage) -> bool:
    if len(_pending_info_candidates(store)) < 2:
        return False
    clauses = _semantic_feedback_clauses(message.text)
    if len(clauses) >= 2:
        return True
    temporal = parse_temporal_update(message.text, reference_date=message.received_at.date())
    return _has_resolution_signal(temporal)


def _missing_info_resolution_update(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> Proposal | None:
    text = message.text.strip()
    temporal = parse_temporal_update(text, reference_date=message.received_at.date())
    if not _has_resolution_signal(temporal):
        return None
    proposal = _find_pending_info_parent(store, text)
    if proposal is None:
        return None

    metadata = _apply_temporal_metadata(
        proposal.metadata,
        temporal,
        actor_id=message.sender_id,
        message_id=message.message_id,
        now=now,
    )
    due_date, scheduled_date, kind = _derive_schedule_fields(proposal, temporal)

    changed = replace(
        proposal,
        kind=kind,
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=temporal.get("time_window", proposal.time_window),
        metadata=metadata,
        updated_at=now,
    )
    change_type = "missing_info_resolved" if not recompute_missing_slots(changed) else "missing_info_partial"
    return _persist_reconciled_change(
        store,
        proposal,
        changed,
        change_body=text,
        actor_id=message.sender_id,
        change_type=change_type,
        now=now,
    )


def _has_resolution_signal(temporal: dict[str, str]) -> bool:
    return bool(
        {
            "due_date",
            "scheduled_date",
            "time_window",
            PARTICIPANTS_KEY,
            EXTERNAL_PARTICIPANTS_KEY,
            PARTICIPANT_LABEL_KEY,
            ATTENDEES_KEY,
            LOCATION_KEY,
        }.intersection(temporal)
    )


def _apply_temporal_metadata(
    metadata: dict[str, str],
    temporal: dict[str, str],
    *,
    actor_id: str,
    message_id: str,
    now: datetime,
) -> dict[str, str]:
    updated = {
        **metadata,
        LAST_STATE_LINKED_UPDATE_TYPE_KEY: "missing_info_resolved",
        "last_resolution_update_message_id": message_id,
        "last_resolution_update_at": now.isoformat(timespec="seconds"),
        "last_resolution_actor_id": actor_id,
    }
    for key in (
        PARTICIPANTS_KEY,
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
            updated[key] = temporal[key]
    if temporal.get("scheduled_date") or temporal.get("due_date") or temporal.get("time_window"):
        for key in (
            DEFERRED_MISSING_SLOTS_KEY,
            DEFERRED_REASON_KEY,
            "deferred_at",
            DEFERRED_UNTIL_KEY,
            DEFERRED_REMINDER_CADENCE_HOURS_KEY,
        ):
            updated.pop(key, None)
    return updated


def _mark_missing_info_approved(
    store: TeamTaskStore,
    proposal: Proposal,
    *,
    actor_id: str,
    now: datetime,
) -> Proposal:
    request = _pending_request_for_actor(
        store,
        proposal.proposal_id,
        actor_id,
        required_approvers=proposal.required_approvers,
    )
    approvals = tuple(sorted(set((*proposal.approvals, actor_id))))
    required = proposal.required_approvers or (actor_id,)
    status = "approved" if set(required).issubset(approvals) else "awaiting_approval"
    changed = replace(proposal, status=status, approvals=approvals, required_approvers=required, updated_at=now)
    # Only consume/decide the pending request when it belongs to the answering actor.  A request
    # raised for a *different* required approver must stay pending so that approver can still decide;
    # consuming it here would strand the proposal in awaiting_approval forever.
    if request is not None and request.approver_id == actor_id:
        decided = replace(request, status="accepted", decided_at=now)
        decision = ApprovalDecision(
            request_id=request.request_id,
            proposal_id=request.proposal_id,
            approver_id=actor_id,
            decision="accepted",
            decided_at=now,
        )
        store.save_approval_request(decided)
        store.save_approval_decision(decision)
        store.append_event("approval.accepted", {"decision": decision, "reconciled": True}, occurred_at=now)
    return changed


def _pending_request_for_actor(
    store: TeamTaskStore,
    proposal_id: str,
    actor_id: str,
    *,
    required_approvers: tuple[str, ...] = (),
):
    requests = store.list_approval_requests(proposal_id=proposal_id, approver_id=actor_id, status="pending")
    if requests:
        return requests[0]
    # Fallback: never adopt a pending request that belongs to an unrelated approver.  Only surface a
    # request whose approver is one of the proposal's required approvers (or the answering actor).
    allowed = set(required_approvers) | {actor_id}
    requests = store.list_approval_requests(proposal_id=proposal_id, status="pending")
    for request in requests:
        if request.approver_id in allowed:
            return request
    return None


def _deferred_missing_info_update(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> Proposal | None:
    text = message.text.strip()
    temporal = parse_temporal_update(text, reference_date=message.received_at.date())
    if not temporal.get("defer_missing_slots") and not temporal.get(LOCATION_OPTIONAL_KEY):
        return None
    proposal = _find_pending_info_parent(store, text)
    if proposal is None:
        return None

    metadata = {
        **proposal.metadata,
        LAST_STATE_LINKED_UPDATE_TYPE_KEY: "missing_info_deferred",
        "last_deferred_update_message_id": message.message_id,
        "last_deferred_update_at": now.isoformat(timespec="seconds"),
    }
    if temporal.get(LOCATION_OPTIONAL_KEY):
        metadata[LOCATION_OPTIONAL_KEY] = temporal[LOCATION_OPTIONAL_KEY]
    if temporal.get("defer_missing_slots"):
        deferred_slots = csv_dedupe(temporal["defer_missing_slots"])
        if deferred_slots:
            metadata[DEFERRED_MISSING_SLOTS_KEY] = ",".join(deferred_slots)
            metadata[DEFERRED_REASON_KEY] = "not_decided"
            metadata["deferred_at"] = now.isoformat(timespec="seconds")
            metadata[DEFERRED_UNTIL_KEY] = default_deferred_until(
                proposal.scheduled_date or proposal.due_date,
                changed_at=now,
            )
            metadata[DEFERRED_REMINDER_CADENCE_HOURS_KEY] = "2"

    changed = replace(proposal, metadata=metadata, updated_at=now)
    changed = replace(changed, missing_slots=recompute_missing_slots(changed), updated_at=now)
    if changed == proposal:
        return None
    store.save_proposal(changed)
    store.append_event(
        "proposal.changed",
        {
            "proposal": changed,
            "change_body": text,
            "actor_id": message.sender_id,
            "reconciled": True,
            "change_type": "missing_info_deferred",
        },
        occurred_at=now,
    )
    return changed


def _find_pending_info_parent(store: TeamTaskStore, text: str) -> Proposal | None:
    candidates = [
        proposal
        for proposal in store.list_proposals()
        if proposal.status == "awaiting_approval"
        and proposal.missing_slots
        and _shares_pending_info_context(proposal, text)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (_date_key(item), item.title, item.proposal_id))[0]


def _shares_pending_info_context(proposal: Proposal, text: str) -> bool:
    if _has_explicit_target_id(text):
        return True
    title_tokens = _clean_context_tokens(proposal.title)
    text_tokens = _clean_context_tokens(text)
    if _strong_target_tokens(title_tokens & text_tokens):
        return True

    # Pronoun-style replies such as "그 회의는 화요일 3시" should fill
    # slots for the open proposal, but a new message that merely shares a
    # generic word like "회의" must not hijack an unrelated pending item.
    if _has_deictic_pointer(text):
        generic_overlap = _generic_context_tokens(proposal.title) & _generic_context_tokens(text)
        if generic_overlap:
            return True

    proposal_context = _clean_context_tokens(
        " ".join(
            value
            for key, value in proposal.metadata.items()
            if key
            in {
                "materials",
                "parent_title",
                "external_owner",
                EXTERNAL_PARTICIPANTS_KEY,
                PARTICIPANT_LABEL_KEY,
            }
        )
    )
    # Named participants alone are too weak: "김담당 선생님과 sample-data sync
    # 미팅" should create a new routine, not mutate an unrelated kickoff
    # meeting that also has 김담당 as an attendee. Require at least two
    # non-generic contextual overlaps outside the title.
    return len(_strong_target_tokens((proposal_context - _PERSON_CONTEXT_TOKENS) & text_tokens)) >= 2


def _has_explicit_target_id(text: str) -> bool:
    normalized = text.lower()
    return "approval/" in normalized or "proposal/" in normalized or "task_management/" in normalized


def _has_deictic_pointer(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(
        token in compact
        for token in (
            "그",
            "그건",
            "그거",
            "그일",
            "그회의",
            "해당",
            "방금",
            "위",
            "앞서",
            "아까",
            "이논의",
            "그논의",
            "그일정",
            "그항목",
        )
    )


_GENERIC_CONTEXT_TOKENS = {
    "회의",
    "미팅",
    "논의",
    "일정",
    "작업",
    "업무",
    "자료",
    "준비",
    "참석자",
    "담당자",
    "장소",
    "시간",
    "날짜",
    "오전",
    "오후",
    "오늘",
    "내일",
    "이번주",
    "다음주",
    "추가",
    "확정",
    "예약",
    "방문",
    "정리",
}

_WEAK_TARGET_TOKENS = {
    "검토",
    "확인",
    "자료",
    "준비",
    "정리",
    "전달",
    "공유",
    "진행",
    "작업",
    "반영",
    "완성",
    "완료",
    "수정",
    "보완",
    "요청",
    "사항",
    "내용",
    "결과",
    "추가",
    "작성",
    "등록",
    "추적",
}


_PERSON_CONTEXT_TOKENS = {
    "나",
    "내가",
    "저",
    "선생님",
    "박사님",
    "센터장님",
    "님",
}


def _strong_target_tokens(tokens: set[str]) -> set[str]:
    return {
        token
        for token in tokens
        if token not in _GENERIC_CONTEXT_TOKENS
        and token not in _PERSON_CONTEXT_TOKENS
        and token not in _WEAK_TARGET_TOKENS
    }


def _generic_context_tokens(text: str) -> set[str]:
    return _raw_hangul_tokens(text) & _GENERIC_CONTEXT_TOKENS


def _clean_context_tokens(text: str) -> set[str]:
    return {
        token
        for token in _raw_hangul_tokens(text)
        if token not in _GENERIC_CONTEXT_TOKENS and token not in _PERSON_CONTEXT_TOKENS
    }


def _raw_hangul_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.split(r"[^0-9A-Za-z가-힣]+", text.lower()):
        if len(token) < 2:
            continue
        variants = {token}
        for suffix in (
            "으로",
            "에서",
            "에게",
            "하고",
            "이랑",
            "까지",
            "부터",
            "에는",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "와",
            "과",
            "로",
            "에",
            "야",
            "쪽",
        ):
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                variants.add(token[: -len(suffix)])
        tokens.update(variants)
    return tokens


@dataclass(frozen=True)
class _PresentationMaterialsPrepMatch:
    message: IncomingMessage
    parent: Proposal
    text: str
    due_date: date


def _match_presentation_materials_prep(
    store: TeamTaskStore,
    message: IncomingMessage,
) -> _PresentationMaterialsPrepMatch | None:
    text = message.text.strip()
    if not _looks_like_presentation_materials_prep(text):
        return None
    due_date = _target_date(text, message.received_at.date())
    parent = _find_presentation_parent(store, text, reference_date=due_date)
    if parent is None or _has_existing_prep(store, parent, text):
        return None
    return _PresentationMaterialsPrepMatch(message=message, parent=parent, text=text, due_date=due_date)


def _apply_presentation_materials_prep(
    store: TeamTaskStore,
    matched: _PresentationMaterialsPrepMatch,
    now: datetime,
) -> tuple[Proposal, ...]:
    message = matched.message
    parent = matched.parent
    text = matched.text
    due_date = matched.due_date
    title = _prep_title(parent)
    assignee = message.sender_id if message.sender_id in {"me", "teammate"} else parent.assigned_to
    if assignee not in {"me", "teammate"}:
        assignee = "me"
    digest = hashlib.sha1(
        f"{parent.proposal_id}:{message.message_id}:presentation-materials:{due_date.isoformat()}".encode("utf-8")
    ).hexdigest()[:12]
    proposal = Proposal(
        proposal_id=f"{parent.proposal_id}/prep/{digest}",
        source_message_id=message.message_id,
        proposer_id=message.sender_id,
        title=title,
        raw_text=text,
        kind="task",
        status="approved",
        assigned_to=assignee,
        task_management_area=parent.task_management_area,
        discussion_id=parent.discussion_id,
        message_id=f"{message.message_id}/reconciled/presentation-materials",
        required_approvers=(assignee,),
        approvals=(assignee,),
        missing_slots=(),
        due_date=due_date,
        time_window=_prep_time_window(text),
        created_at=now,
        updated_at=now,
        metadata={
            **_source_metadata(message.message_id),
            "parent_proposal_id": parent.proposal_id,
            LINK_TYPE_KEY: LINK_PREP_SUBTASK,
            "materials": "발표자료",
            "source_text_hash": _text_hash(text),
            "reconciler": "presentation_materials_prep.v1",
            "reconciled_from": "slack_dm",
        },
    )
    store.save_proposal(proposal)
    store.append_event("proposal.created", {"proposal": proposal, "generated": True, "reconciled": True}, occurred_at=now)
    store.append_event(
        "approval.accepted",
        {"proposal_id": proposal.proposal_id, "approver_id": assignee, "auto": True, "reconciled": True},
        occurred_at=now,
    )
    store.append_event("proposal.approved", {"proposal": proposal, "generated": True, "reconciled": True}, occurred_at=now)
    return (proposal,)


@dataclass(frozen=True)
class _OfficialPresentationScheduleMatch:
    message: IncomingMessage
    parent: Proposal
    text: str
    scheduled_date: date
    time_window: str
    official_title: str
    location: str


def _match_official_presentation_schedule(
    store: TeamTaskStore,
    message: IncomingMessage,
) -> _OfficialPresentationScheduleMatch | None:
    text = message.text.strip()
    parsed = _parse_official_presentation_schedule(text)
    if parsed is None:
        return None
    scheduled_date, time_window, official_title, location = parsed
    parent = _find_official_schedule_parent(store, text, scheduled_date=scheduled_date)
    if parent is None:
        return None
    return _OfficialPresentationScheduleMatch(
        message=message,
        parent=parent,
        text=text,
        scheduled_date=scheduled_date,
        time_window=time_window,
        official_title=official_title,
        location=location,
    )


def _apply_official_presentation_schedule(
    store: TeamTaskStore,
    matched: _OfficialPresentationScheduleMatch,
    now: datetime,
) -> tuple[Proposal, ...]:
    message = matched.message
    parent = matched.parent
    text = matched.text
    scheduled_date = matched.scheduled_date
    time_window = matched.time_window
    official_title = matched.official_title
    location = matched.location

    metadata = {
        **parent.metadata,
        "official_title": official_title,
        LOCATION_KEY: location,
        "last_schedule_update_message_id": message.message_id,
        "last_schedule_update_at": now.isoformat(timespec="seconds"),
        "schedule_update_source_text_hash": _text_hash(text),
        **{
            f"schedule_update_{key}": value
            for key, value in _source_metadata(message.message_id).items()
        },
    }
    metadata.pop(LOCATION_OPTIONAL_KEY, None)
    title = _merged_official_event_title(parent, official_title)
    if (
        parent.title == title
        and parent.scheduled_date == scheduled_date
        and parent.time_window == time_window
        and parent.metadata.get(LOCATION_KEY) == location
        and parent.metadata.get("last_schedule_update_message_id") == message.message_id
    ):
        return ()

    updated = replace(
        parent,
        title=title,
        kind="event",
        scheduled_date=scheduled_date,
        due_date=None,
        time_window=time_window,
        missing_slots=(),
        metadata=metadata,
        updated_at=now,
    )
    store.save_proposal(updated)
    store.append_event(
        "proposal.changed",
        {
            "proposal": updated,
            "change_body": text,
            "actor_id": message.sender_id,
            "reconciled": True,
            "change_type": "official_presentation_schedule",
        },
        occurred_at=now,
    )
    return (updated,)


RECONCILER_RULES: tuple[ReconcilerRule, ...] = (
    ReconcilerRule(
        name="presentation_materials_prep.v1",
        phase="create_followup",
        priority=100,
        match=_match_presentation_materials_prep,
        apply=_apply_presentation_materials_prep,
    ),
    ReconcilerRule(
        name="official_presentation_schedule.v1",
        phase="update_existing",
        priority=100,
        match=_match_official_presentation_schedule,
        apply=_apply_official_presentation_schedule,
    ),
)


def _parse_official_presentation_schedule(text: str) -> tuple[date, str, str, str] | None:
    if not ("발표일자" in text and "심의" in text):
        return None
    match = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    scheduled_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    time_window = f"{int(match.group(4)):02d}:{int(match.group(5)):02d}"
    official_title = _official_title_from_lines(text)
    location = _official_location_from_lines(text)
    if not official_title or not location:
        return None
    return scheduled_date, time_window, official_title, location


def _official_title_from_lines(text: str) -> str:
    for line in _non_empty_lines(text):
        if "발표일자" in line:
            continue
        if re.search(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}", line):
            continue
        if "심의" in line:
            return line.replace("심의일정", "심의").strip()
    return ""


def _official_location_from_lines(text: str) -> str:
    ignored = {"발표일자"}
    candidates: list[str] = []
    for line in _non_empty_lines(text):
        if line in ignored or "심의" in line or re.search(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}", line):
            continue
        if any(token in line for token in ("로", "길", "층", "홀", "컨벤션", "호텔", "센터", "회의실")):
            candidates.append(line)
    return max(candidates, key=len) if candidates else ""


def _find_official_schedule_parent(
    store: TeamTaskStore,
    text: str,
    *,
    scheduled_date: date,
) -> Proposal | None:
    candidates = [
        proposal
        for proposal in store.list_proposals()
        if proposal.kind == "event"
        and proposal.status in {"approved", "applied", "done", "awaiting_approval"}
        and _shares_official_schedule_context(proposal, text)
    ]
    same_day = [proposal for proposal in candidates if proposal.scheduled_date == scheduled_date]
    if same_day:
        return sorted(same_day, key=lambda item: (item.time_window, item.title))[0]
    if candidates:
        return sorted(candidates, key=lambda item: (_date_key(item), item.time_window, item.title))[0]
    return None


def _shares_official_schedule_context(proposal: Proposal, text: str) -> bool:
    haystack = f"{proposal.title} {proposal.raw_text} {text}"
    return "심의" in haystack and ("발표" in haystack or "발표일자" in haystack)


def _merged_official_event_title(parent: Proposal, official_title: str) -> str:
    title = official_title.strip()
    if "배석" in parent.title and "배석" not in title:
        if "발표" not in title:
            title = f"{title} 발표"
        title = f"{title} 배석"
    return title


def _non_empty_lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _looks_like_presentation_materials_prep(text: str) -> bool:
    normalized = text.replace(" ", "").lower()
    has_materials = "발표자료" in normalized or "발표문건" in normalized
    has_prep_action = any(token in normalized for token in ("제작", "준비", "만들", "정리"))
    return has_materials and has_prep_action


def _find_presentation_parent(store: TeamTaskStore, text: str, *, reference_date: date) -> Proposal | None:
    candidates = [
        proposal
        for proposal in store.list_proposals()
        if proposal.kind == "event"
        and proposal.status in {"approved", "applied", "done"}
        and (_shares_presentation_context(proposal, text))
    ]
    exact = [proposal for proposal in candidates if proposal.scheduled_date == reference_date]
    if exact:
        return sorted(exact, key=lambda item: (item.time_window, item.title))[0]
    if candidates:
        return sorted(candidates, key=lambda item: (_date_key(item), item.time_window, item.title))[0]
    return None


def _shares_presentation_context(proposal: Proposal, text: str) -> bool:
    haystack = f"{proposal.title} {proposal.raw_text} {text}".lower()
    return "리뷰위원회" in haystack and ("발표" in haystack or "presentation" in haystack)


def _has_existing_prep(store: TeamTaskStore, parent: Proposal, text: str) -> bool:
    text_hash = _text_hash(text)
    for proposal in store.list_proposals():
        if proposal.metadata.get(LINK_TYPE_KEY) != LINK_PREP_SUBTASK:
            continue
        if proposal.metadata.get("parent_proposal_id") != parent.proposal_id:
            continue
        if proposal.metadata.get("materials") == "발표자료":
            return True
        if proposal.metadata.get("source_text_hash") == text_hash:
            return True
    return False


def _target_date(text: str, reference_date: date) -> date:
    return relative_date(text, reference_date)


def _prep_time_window(text: str) -> str:
    if "아침" in text or "오전" in text:
        return "morning"
    if "오후" in text:
        return "afternoon"
    return ""


def _prep_title(parent: Proposal) -> str:
    if "리뷰위원회" in parent.title:
        return "리뷰위원회 발표자료 제작"
    return f"{parent.title} 발표자료 제작"


def _date_key(proposal: Proposal) -> str:
    proposal_date = proposal.scheduled_date or proposal.due_date
    return proposal_date.isoformat() if proposal_date else "9999-12-31"

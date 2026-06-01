from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import hashlib
import os
import re

from .conflict_policy import apply_conflict_resolution_feedback
from .deferred_policy import csv_dedupe, default_deferred_until
from .domain import ApprovalDecision, IncomingMessage, OrchestrationResult, Proposal
from .discussion_adapter import parse_temporal_update
from .slot_validator import missing_slots_for_proposal
from .store import TeamTaskStore


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
    presentation_prep = _presentation_materials_prep(store, message, now=now)
    if presentation_prep is not None:
        created.append(presentation_prep)
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


def apply_state_linked_update(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    updated_at: datetime | None = None,
) -> tuple[Proposal, ...]:
    """Apply deterministic updates to existing proposals before creating new ones.

    The important operating principle is semantic, not rule-shaped: a follow-up
    Slack DM is interpreted against the open task_management state before it is parsed
    as a new task.  When multiple proposals are pending, split the answer into
    meaning-bearing clauses, resolve each clause to a concrete proposal, and only
    then apply slots.  If a clause cannot be mapped confidently, do not mutate an
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
            schedule_update = _official_presentation_schedule_update(store, message, now=now)
            if schedule_update is not None:
                updated.append(schedule_update)
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

    This is the deterministic version of the intended LLM operation.  It keeps
    the same invariant the future semantic agent must keep: propose targeted
    patches with evidence, never spray one parsed date/time across all pending
    tasks.
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
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1].updated_at or item[1].created_at or datetime.min), reverse=True)
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if best_score < 8:
        return None
    if runner_up and best_score - runner_up < 3:
        return None
    return best


def _feedback_clause_score(proposal: Proposal, clause: str) -> int:
    normalized = clause.lower()
    score = 0
    if _is_this_discussion_clause(normalized):
        if proposal.metadata.get("conflict_detected") == "true" or "conflict_resolution" in proposal.missing_slots:
            score += 80
        if any(token in f"{proposal.title} {proposal.raw_text}".lower() for token in ("논의", "할일", "할 일", "정리")):
            score += 10
    title_tokens = _clean_context_tokens(proposal.title)
    raw_tokens = _clean_context_tokens(proposal.raw_text)
    clause_tokens = _clean_context_tokens(clause)
    title_overlap = _strong_target_tokens(title_tokens & clause_tokens)
    raw_overlap = _strong_target_tokens((raw_tokens - title_tokens) & clause_tokens)
    context_overlap = _strong_target_tokens(_target_context_tokens(proposal) & clause_tokens)
    score += 12 * len(title_overlap)
    score += 5 * len(raw_overlap)
    score += 4 * len(context_overlap)
    if "차주" in normalized and any(token in f"{proposal.title} {proposal.raw_text}" for token in ("차주", "복귀")):
        score += 25
    if "복귀" in normalized and "복귀" in f"{proposal.title} {proposal.raw_text}":
        score += 20
    if "담당" in normalized and "assigned_to" in proposal.missing_slots:
        score += 3
    if any(token in normalized for token in ("일시", "언제", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")) and any(
        slot in proposal.missing_slots for slot in ("date", "time", "exact_date")
    ):
        score += 3
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


def _semantic_tokens(text: str) -> set[str]:
    stopwords = {
        "오늘",
        "내일",
        "모레",
        "이번주",
        "다음주",
        "차주",
        "담당자",
        "담당자는",
        "일시는",
        "일시",
        "좋겠네",
        "할",
        "일",
        "이",
        "그",
        "저",
        "것",
        "는",
        "은",
    }
    tokens: set[str] = set()
    for token in re.split(r"[^0-9A-Za-z가-힣]+", text.lower()):
        if len(token) < 2 or token in stopwords:
            continue
        tokens.update(item for item in _semantic_token_variants(token) if len(item) >= 2 and item not in stopwords)
    return tokens


_GENERIC_FEEDBACK_TOKENS = {
    "실사용테스트",
    "테스트",
    "확인",
    "필요",
    "일정",
    "작업",
    "담당",
    "담당자",
    "정하기",
    "정하자",
}


def _semantic_token_variants(token: str) -> set[str]:
    variants = {token}
    for suffix in (
        "으로",
        "에는",
        "에서",
        "에게",
        "까지",
        "부터",
        "하고",
        "이랑",
        "랑",
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "로",
        "와",
        "과",
        "쪽",
    ):
        if token.endswith(suffix) and len(token) > len(suffix) + 1:
            variants.add(token[: -len(suffix)])
    return variants


def _target_semantic_tokens(proposal: Proposal) -> set[str]:
    metadata_text = " ".join(
        value
        for key, value in proposal.metadata.items()
        if key
        in {
            "participant_label",
            "external_owner",
            "external_participants",
            "materials",
            "location",
            "parent_title",
        }
    )
    return _semantic_tokens(f"{proposal.title} {proposal.raw_text} {metadata_text}")


def _target_context_tokens(proposal: Proposal) -> set[str]:
    metadata_text = " ".join(
        value
        for key, value in proposal.metadata.items()
        if key
        in {
            "materials",
            "parent_title",
            "external_owner",
            "external_participants",
            "participant_label",
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

    due_date = proposal.due_date
    scheduled_date = proposal.scheduled_date
    if temporal.get("due_date"):
        due_date = datetime.fromisoformat(temporal["due_date"]).date()
        scheduled_date = None
    if temporal.get("scheduled_date"):
        scheduled_date = datetime.fromisoformat(temporal["scheduled_date"]).date()
        due_date = None
    prefers_due_task = scheduled_date is not None and _semantic_clause_prefers_due_task(proposal, clause)
    if prefers_due_task:
        due_date = scheduled_date
        scheduled_date = None
        metadata["semantic_schedule_interpretation"] = "due_task"
    if not temporal.get("location") and _looks_like_stale_location(metadata.get("location", "")):
        metadata.pop("location", None)

    kind = proposal.kind
    missing_slots = proposal.missing_slots
    if _semantic_clause_reclassifies_conflict_as_task(proposal, clause, temporal):
        kind = "task"
        scheduled_date = None
        metadata["semantic_correction"] = "not_blocking_event_due_task"
        metadata["previous_conflict_detected"] = metadata.pop("conflict_detected", "")
        metadata.pop("conflict_with_proposal_ids", None)
        metadata.pop("conflict_policy", None)
        metadata.pop("blocks_in_person", None)
        metadata.pop("event_scope", None)
        missing_slots = tuple(slot for slot in missing_slots if slot != "conflict_resolution")
    elif prefers_due_task:
        kind = "task"
    elif scheduled_date is not None:
        kind = "event"
    elif due_date is not None and kind == "question":
        kind = "task"

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
    changed = replace(changed, missing_slots=missing_slots_for_proposal(changed), updated_at=now)
    if not changed.missing_slots:
        changed = _mark_missing_info_approved(store, changed, actor_id=actor_id, now=now)
    elif changed == proposal:
        return None

    store.save_proposal(changed)
    store.append_event(
        "proposal.changed",
        {
            "proposal": changed,
            "change_body": clause,
            "actor_id": actor_id,
            "reconciled": True,
            "change_type": "semantic_pending_feedback",
        },
        occurred_at=now,
    )
    if changed.status == "approved":
        store.append_event("proposal.approved", {"proposal": changed, "reconciled": True}, occurred_at=now)
    return changed


def _semantic_clause_reclassifies_conflict_as_task(
    proposal: Proposal,
    clause: str,
    temporal: dict[str, str],
) -> bool:
    if proposal.metadata.get("conflict_detected") != "true" and "conflict_resolution" not in proposal.missing_slots:
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
    due_date = proposal.due_date
    scheduled_date = proposal.scheduled_date
    if temporal.get("due_date"):
        due_date = datetime.fromisoformat(temporal["due_date"]).date()
        scheduled_date = None
    if temporal.get("scheduled_date"):
        scheduled_date = datetime.fromisoformat(temporal["scheduled_date"]).date()
        due_date = None

    kind = proposal.kind
    if scheduled_date is not None:
        kind = "event"
    elif due_date is not None and kind == "question":
        kind = "task"

    changed = replace(
        proposal,
        kind=kind,
        due_date=due_date,
        scheduled_date=scheduled_date,
        time_window=temporal.get("time_window", proposal.time_window),
        metadata=metadata,
        updated_at=now,
    )
    missing_slots = missing_slots_for_proposal(changed)
    changed = replace(changed, missing_slots=missing_slots, updated_at=now)
    if not missing_slots:
        changed = _mark_missing_info_approved(store, changed, actor_id=message.sender_id, now=now)
    elif changed == proposal:
        return None

    store.save_proposal(changed)
    store.append_event(
        "proposal.changed",
        {
            "proposal": changed,
            "change_body": text,
            "actor_id": message.sender_id,
            "reconciled": True,
            "change_type": "missing_info_resolved" if not missing_slots else "missing_info_partial",
        },
        occurred_at=now,
    )
    if changed.status == "approved":
        store.append_event("proposal.approved", {"proposal": changed, "reconciled": True}, occurred_at=now)
    return changed


def _has_resolution_signal(temporal: dict[str, str]) -> bool:
    return bool(
        {
            "due_date",
            "scheduled_date",
            "time_window",
            "participants",
            "external_participants",
            "participant_label",
            "attendees",
            "location",
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
        "last_state_linked_update_type": "missing_info_resolved",
        "last_resolution_update_message_id": message_id,
        "last_resolution_update_at": now.isoformat(timespec="seconds"),
        "last_resolution_actor_id": actor_id,
    }
    for key in (
        "participants",
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
            updated[key] = temporal[key]
    if temporal.get("scheduled_date") or temporal.get("due_date") or temporal.get("time_window"):
        for key in (
            "deferred_missing_slots",
            "deferred_reason",
            "deferred_at",
            "deferred_until",
            "deferred_reminder_cadence_hours",
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
    request = _pending_request_for_actor(store, proposal.proposal_id, actor_id)
    approvals = tuple(sorted(set((*proposal.approvals, actor_id))))
    required = proposal.required_approvers or (actor_id,)
    status = "approved" if set(required).issubset(approvals) else "awaiting_approval"
    changed = replace(proposal, status=status, approvals=approvals, required_approvers=required, updated_at=now)
    if request is not None:
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


def _pending_request_for_actor(store: TeamTaskStore, proposal_id: str, actor_id: str):
    requests = store.list_approval_requests(proposal_id=proposal_id, approver_id=actor_id, status="pending")
    if requests:
        return requests[0]
    requests = store.list_approval_requests(proposal_id=proposal_id, status="pending")
    return requests[0] if requests else None


def _deferred_missing_info_update(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> Proposal | None:
    text = message.text.strip()
    temporal = parse_temporal_update(text, reference_date=message.received_at.date())
    if not temporal.get("defer_missing_slots") and not temporal.get("location_optional"):
        return None
    proposal = _find_pending_info_parent(store, text)
    if proposal is None:
        return None

    metadata = {
        **proposal.metadata,
        "last_state_linked_update_type": "missing_info_deferred",
        "last_deferred_update_message_id": message.message_id,
        "last_deferred_update_at": now.isoformat(timespec="seconds"),
    }
    if temporal.get("location_optional"):
        metadata["location_optional"] = temporal["location_optional"]
    if temporal.get("defer_missing_slots"):
        deferred_slots = csv_dedupe(temporal["defer_missing_slots"])
        if deferred_slots:
            metadata["deferred_missing_slots"] = ",".join(deferred_slots)
            metadata["deferred_reason"] = "not_decided"
            metadata["deferred_at"] = now.isoformat(timespec="seconds")
            metadata["deferred_until"] = default_deferred_until(
                proposal.scheduled_date or proposal.due_date,
                changed_at=now,
            )
            metadata["deferred_reminder_cadence_hours"] = "2"

    changed = replace(proposal, metadata=metadata, updated_at=now)
    changed = replace(changed, missing_slots=missing_slots_for_proposal(changed), updated_at=now)
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
                "external_participants",
                "participant_label",
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


def _presentation_materials_prep(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> Proposal | None:
    text = message.text.strip()
    if not _looks_like_presentation_materials_prep(text):
        return None
    parent = _find_presentation_parent(store, text, reference_date=_target_date(text, message.received_at.date()))
    if parent is None or _has_existing_prep(store, parent, text):
        return None

    due_date = _target_date(text, message.received_at.date())
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
            "link_type": "prep_subtask",
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
    return proposal


def _official_presentation_schedule_update(
    store: TeamTaskStore,
    message: IncomingMessage,
    *,
    now: datetime,
) -> Proposal | None:
    text = message.text.strip()
    parsed = _parse_official_presentation_schedule(text)
    if parsed is None:
        return None
    scheduled_date, time_window, official_title, location = parsed
    parent = _find_official_schedule_parent(store, text, scheduled_date=scheduled_date)
    if parent is None:
        return None

    metadata = {
        **parent.metadata,
        "official_title": official_title,
        "location": location,
        "last_schedule_update_message_id": message.message_id,
        "last_schedule_update_at": now.isoformat(timespec="seconds"),
        "schedule_update_source_text_hash": _text_hash(text),
        **{
            f"schedule_update_{key}": value
            for key, value in _source_metadata(message.message_id).items()
        },
    }
    metadata.pop("location_optional", None)
    title = _merged_official_event_title(parent, official_title)
    if (
        parent.title == title
        and parent.scheduled_date == scheduled_date
        and parent.time_window == time_window
        and parent.metadata.get("location") == location
        and parent.metadata.get("last_schedule_update_message_id") == message.message_id
    ):
        return None

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
    return updated


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
        if proposal.metadata.get("link_type") != "prep_subtask":
            continue
        if proposal.metadata.get("parent_proposal_id") != parent.proposal_id:
            continue
        if proposal.metadata.get("materials") == "발표자료":
            return True
        if proposal.metadata.get("source_text_hash") == text_hash:
            return True
    return False


def _target_date(text: str, reference_date: date) -> date:
    if "내일" in text:
        return reference_date + timedelta(days=1)
    if "어제" in text:
        return reference_date - timedelta(days=1)
    return reference_date


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


def _text_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _source_metadata(message_id: str) -> dict[str, str]:
    if not message_id.startswith("slack/"):
        return {}
    parts = message_id.split("/")
    if len(parts) < 3:
        return {"source_provider": "slack"}
    return {
        "source_provider": "slack",
        "source_channel": parts[1],
        "source_ts": parts[2],
    }

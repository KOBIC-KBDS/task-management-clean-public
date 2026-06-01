from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .domain import Proposal


RELATION_TYPE_KEY = "relation_type"
RELATION_WORKFLOW_STEP = "workflow_step"
RELATION_CHILD_TASK = "child_task"
RELATION_PREP_SUBTASK = "prep_subtask"

WORKFLOW_ID_KEY = "workflow_id"
WORKFLOW_TITLE_KEY = "workflow_title"
WORKFLOW_ROLE_KEY = "workflow_role"
WORKFLOW_PARENT_ROLE = "parent"
WORKFLOW_GROUP_ID_KEY = "workflow_group_id"
WORKFLOW_GROUP_REQUEST_ID_KEY = "workflow_group_request_id"
WORKFLOW_GROUP_CHILD_IDS_KEY = "workflow_group_child_ids"
WORKFLOW_SEPARATE_CHILD_IDS_KEY = "workflow_separate_child_ids"
WORKFLOW_SOURCE_KEY = "workflow_source_key"
PARENT_PROPOSAL_ID_KEY = "parent_proposal_id"
PARENT_SOURCE_KEY = "parent_source_key"
DEPENDS_ON_PROPOSAL_IDS_KEY = "depends_on_proposal_ids"
DEPENDS_ON_PROPOSAL_ID_KEY = "depends_on_proposal_id"
DEPENDS_ON_SOURCE_KEYS_KEY = "depends_on_source_keys"
STEP_INDEX_KEY = "step_index"
STEP_COUNT_KEY = "step_count"

REQUIRES_SEPARATE_APPROVAL_KEY = "requires_separate_approval"
RISK_LEVEL_KEY = "risk_level"
RISK_REASON_KEY = "risk_reason"
RISK_EVIDENCE_KEY = "risk_evidence"

ALLOWED_RISK_REASONS = frozenset(
    {
        "",
        "ambiguous",
        "external_send",
        "destructive",
        "credential",
        "privacy",
        "money",
        "deadline_change",
        "unknown",
    }
)


@dataclass(frozen=True)
class RelationValidationError:
    code: str
    proposal_id: str
    detail: str


@dataclass(frozen=True)
class WorkflowProjection:
    parent: Proposal
    children: tuple[Proposal, ...]
    current_next_step: Proposal | None
    blocking_dependencies: tuple[Proposal, ...]
    rollup_status: str


def parent_proposal_id(proposal: Proposal) -> str:
    return proposal.metadata.get(PARENT_PROPOSAL_ID_KEY, "").strip()


def workflow_id(proposal: Proposal) -> str:
    return proposal.metadata.get(WORKFLOW_ID_KEY, "").strip()


def workflow_title(proposal: Proposal) -> str:
    return proposal.metadata.get(WORKFLOW_TITLE_KEY, "").strip()


def is_workflow_child(proposal: Proposal) -> bool:
    return bool(parent_proposal_id(proposal))


def is_workflow_parent(proposal: Proposal, proposals: list[Proposal] | tuple[Proposal, ...]) -> bool:
    if proposal.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE:
        return True
    return any(parent_proposal_id(item) == proposal.proposal_id for item in proposals)


def depends_on_proposal_ids(proposal: Proposal) -> tuple[str, ...]:
    raw = (
        proposal.metadata.get(DEPENDS_ON_PROPOSAL_IDS_KEY)
        or proposal.metadata.get(DEPENDS_ON_PROPOSAL_ID_KEY)
        or proposal.metadata.get("depends_on")
        or ""
    )
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def blocking_dependencies(proposal: Proposal, proposals_by_id: dict[str, Proposal]) -> tuple[Proposal, ...]:
    blockers: list[Proposal] = []
    for proposal_id in depends_on_proposal_ids(proposal):
        dependency = proposals_by_id.get(proposal_id)
        if dependency is not None and not is_relation_complete(dependency):
            blockers.append(dependency)
    return tuple(blockers)


def child_proposals(parent: Proposal, proposals: list[Proposal] | tuple[Proposal, ...]) -> tuple[Proposal, ...]:
    children = [proposal for proposal in proposals if parent_proposal_id(proposal) == parent.proposal_id]
    return order_child_proposals(children)


def order_child_proposals(children: list[Proposal] | tuple[Proposal, ...]) -> tuple[Proposal, ...]:
    """Order workflow children by explicit step first, dependency second, then deadline."""

    children_tuple = tuple(children)
    if not children_tuple:
        return ()
    if any(item.metadata.get(STEP_INDEX_KEY, "").strip() for item in children_tuple):
        return tuple(sorted(children_tuple, key=relation_sort_key))

    by_id = {item.proposal_id: item for item in children_tuple}
    remaining = set(by_id)
    ordered: list[Proposal] = []
    while remaining:
        ready = [
            by_id[item_id]
            for item_id in remaining
            if not any(dep in remaining for dep in depends_on_proposal_ids(by_id[item_id]))
        ]
        if not ready:
            ready = [by_id[item_id] for item_id in remaining]
        ready.sort(key=_deadline_sort_key)
        chosen = ready[0]
        ordered.append(chosen)
        remaining.remove(chosen.proposal_id)
    return tuple(ordered)


def is_relation_complete(proposal: Proposal) -> bool:
    return proposal.status in {"done", "rejected"}


def relation_sort_key(proposal: Proposal) -> tuple[int, int, str, int, str]:
    """Step-first child ordering; date/time only break ties."""

    step_index = proposal.metadata.get(STEP_INDEX_KEY, "").strip()
    if step_index.isdigit():
        step_rank = int(step_index)
        has_step = 0
    else:
        step_rank = 999_999
        has_step = 1
    proposal_date = proposal.scheduled_date or proposal.due_date
    return (
        has_step,
        step_rank,
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        _time_sort_minutes(proposal.time_window),
        proposal.title,
    )


def step_label(proposal: Proposal) -> str:
    step_index = proposal.metadata.get(STEP_INDEX_KEY, "")
    step_count = proposal.metadata.get(STEP_COUNT_KEY, "")
    if step_index and step_count:
        return f"{step_index}/{step_count}"
    if step_index:
        return f"{step_index}단계"
    return ""


def requires_separate_approval(proposal: Proposal) -> bool:
    if proposal.metadata.get(REQUIRES_SEPARATE_APPROVAL_KEY) == "true":
        return True
    risk_level = proposal.metadata.get(RISK_LEVEL_KEY, "").strip().lower()
    risk_reason = proposal.metadata.get(RISK_REASON_KEY, "").strip().lower()
    return risk_level in {"medium", "high", "critical"} or (
        risk_reason not in ALLOWED_RISK_REASONS and bool(risk_reason)
    )


def validate_workflow_relations(
    proposals: list[Proposal] | tuple[Proposal, ...],
    *,
    existing_proposals: list[Proposal] | tuple[Proposal, ...] = (),
) -> tuple[RelationValidationError, ...]:
    """Validate relation metadata before any workflow batch proposal is saved."""

    all_proposals = tuple(existing_proposals) + tuple(proposals)
    by_id = {proposal.proposal_id: proposal for proposal in all_proposals}
    new_ids = {proposal.proposal_id for proposal in proposals}
    errors: list[RelationValidationError] = []
    children_by_parent: dict[str, list[Proposal]] = {}

    for proposal in all_proposals:
        parent_id = parent_proposal_id(proposal)
        if parent_id:
            if proposal.proposal_id in new_ids and parent_id not in by_id:
                errors.append(
                    RelationValidationError(
                        code="missing_parent",
                        proposal_id=proposal.proposal_id,
                        detail=parent_id,
                    )
                )
            children_by_parent.setdefault(parent_id, []).append(proposal)
        if proposal.proposal_id not in new_ids:
            continue
        for dependency_id in depends_on_proposal_ids(proposal):
            if dependency_id not in by_id:
                errors.append(
                    RelationValidationError(
                        code="missing_dependency",
                        proposal_id=proposal.proposal_id,
                        detail=dependency_id,
                    )
                )
        errors.extend(_risk_validation_errors(proposal))

    for parent_id, children in children_by_parent.items():
        seen: dict[str, str] = {}
        for child in children:
            step = child.metadata.get(STEP_INDEX_KEY, "").strip()
            if not step:
                continue
            if step in seen:
                if child.proposal_id not in new_ids and seen[step] not in new_ids:
                    continue
                errors.append(
                    RelationValidationError(
                        code="duplicate_step_index",
                        proposal_id=child.proposal_id,
                        detail=f"{parent_id}:{step}:{seen[step]}",
                    )
                )
            else:
                seen[step] = child.proposal_id
    return tuple(errors)


def resolve_same_batch_relation_metadata(proposals: tuple[Proposal, ...]) -> tuple[Proposal, ...]:
    """Resolve optional source-key relation metadata to final proposal ids.

    In this adapter `Proposal.proposal_id` currently equals the source key, but
    keeping this projection boundary prevents future id-generation changes from
    leaking into the semantic contract.
    """

    from dataclasses import replace

    by_source_key = {
        (proposal.metadata.get(WORKFLOW_SOURCE_KEY) or proposal.proposal_id): proposal.proposal_id
        for proposal in proposals
    }
    by_source_key.update({proposal.proposal_id: proposal.proposal_id for proposal in proposals})

    resolved: list[Proposal] = []
    for proposal in proposals:
        metadata = dict(proposal.metadata)
        parent_source = metadata.pop(PARENT_SOURCE_KEY, "")
        if parent_source and not metadata.get(PARENT_PROPOSAL_ID_KEY):
            metadata[PARENT_PROPOSAL_ID_KEY] = by_source_key.get(parent_source, parent_source)
        dependency_sources = metadata.pop(DEPENDS_ON_SOURCE_KEYS_KEY, "")
        if dependency_sources and not metadata.get(DEPENDS_ON_PROPOSAL_IDS_KEY):
            metadata[DEPENDS_ON_PROPOSAL_IDS_KEY] = ",".join(
                by_source_key.get(item.strip(), item.strip())
                for item in dependency_sources.split(",")
                if item.strip()
            )
        resolved.append(replace(proposal, metadata=metadata))
    return tuple(resolved)


def workflow_projection(
    parent: Proposal,
    proposals: list[Proposal] | tuple[Proposal, ...],
) -> WorkflowProjection:
    proposals_by_id = {proposal.proposal_id: proposal for proposal in proposals}
    children = child_proposals(parent, proposals)
    current: Proposal | None = None
    current_blockers: tuple[Proposal, ...] = ()
    for child in children:
        if is_relation_complete(child):
            continue
        blockers = blocking_dependencies(child, proposals_by_id)
        if blockers:
            if current is None:
                current = child
                current_blockers = blockers
            continue
        current = child
        current_blockers = ()
        break

    if children and all(is_relation_complete(child) for child in children):
        rollup = "complete"
    elif current_blockers:
        rollup = "blocked"
    elif any(child.status == "awaiting_approval" for child in children):
        rollup = "awaiting_approval"
    elif children:
        rollup = "in_progress"
    else:
        rollup = parent.status
    return WorkflowProjection(
        parent=parent,
        children=children,
        current_next_step=current,
        blocking_dependencies=current_blockers,
        rollup_status=rollup,
    )


def workflow_group_id(parent: Proposal) -> str:
    return parent.metadata.get(WORKFLOW_GROUP_ID_KEY) or f"workflow-group/{parent.proposal_id}"


def _risk_validation_errors(proposal: Proposal) -> tuple[RelationValidationError, ...]:
    risk_level = proposal.metadata.get(RISK_LEVEL_KEY, "").strip().lower()
    risk_reason = proposal.metadata.get(RISK_REASON_KEY, "").strip().lower()
    separate = proposal.metadata.get(REQUIRES_SEPARATE_APPROVAL_KEY) == "true"
    errors: list[RelationValidationError] = []
    if risk_level in {"high", "critical"} and not separate:
        errors.append(
            RelationValidationError(
                code="risky_child_requires_separate_approval",
                proposal_id=proposal.proposal_id,
                detail=risk_level,
            )
        )
    if risk_reason and risk_reason not in ALLOWED_RISK_REASONS and not separate:
        errors.append(
            RelationValidationError(
                code="unknown_risk_reason_requires_separate_approval",
                proposal_id=proposal.proposal_id,
                detail=risk_reason,
            )
        )
    return tuple(errors)


def _deadline_sort_key(proposal: Proposal) -> tuple[str, int, str]:
    proposal_date: date | None = proposal.scheduled_date or proposal.due_date
    return (
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        _time_sort_minutes(proposal.time_window),
        proposal.title,
    )


def _time_sort_minutes(value: str) -> int:
    if not value:
        return 24 * 60 + 1
    compact = value.lower().replace(" ", "")
    if compact.startswith("오전"):
        return 9 * 60
    if compact.startswith("오후"):
        return 13 * 60
    if compact in {"퇴근전", "퇴근전까지"}:
        return 18 * 60
    hour = compact.split(":", 1)[0]
    if hour.isdigit():
        return int(hour) * 60
    return 24 * 60

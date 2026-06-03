from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
import hashlib
import re

from .domain import Proposal
from .relations import (
    DEPENDS_ON_PROPOSAL_IDS_KEY,
    PARENT_PROPOSAL_ID_KEY,
    STEP_COUNT_KEY,
    STEP_INDEX_KEY,
    WORKFLOW_ID_KEY,
    WORKFLOW_PARENT_ROLE,
    WORKFLOW_ROLE_KEY,
    WORKFLOW_TITLE_KEY,
    depends_on_proposal_ids,
    parent_proposal_id,
)


@dataclass(frozen=True)
class WorkflowGraphNormalization:
    proposals: tuple[Proposal, ...]
    updated_existing: tuple[Proposal, ...] = ()


_SERIES_TITLE_RE = re.compile(r"(?:제\s*)?(\d+)\s*회\s*([^\s·,，:：/()]+)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*|[가-힣]{2,}")

_FOLLOW_UP_MARKERS = (
    "후속",
    "완료보고",
    "완료 보고",
    "결과",
    "공유",
    "메일",
    "발송",
    "보고서",
    "회의록",
    "follow-up",
    "followup",
    "report",
    "send",
    "mail",
)
_PLANNING_MARKERS = (
    "사전",
    "리뷰",
    "논의",
    "일정 결정",
    "주제",
    "날짜",
    "준비",
    "미팅",
    "meeting",
    "review",
    "prep",
    "planning",
)
_TOPIC_STOPWORDS = {
    "제",
    "회",
    "일정",
    "결정",
    "논의",
    "주제",
    "사전",
    "미팅",
    "회의",
    "후속",
    "완료",
    "보고",
    "보고서",
    "메일",
    "발송",
    "자료",
    "결과",
    "공유",
    "task",
    "event",
    "meeting",
    "review",
    "prep",
    "send",
    "mail",
    "report",
}


def normalize_new_proposal_graph(
    existing_proposals: tuple[Proposal, ...],
    new_proposals: tuple[Proposal, ...],
    *,
    normalized_at: datetime,
) -> WorkflowGraphNormalization:
    """Normalize newly-created proposal graph relations before persistence.

    This is intentionally narrow and deterministic.  The operating agent still
    owns semantic interpretation; this layer only prevents structurally awkward
    graph shapes such as a post-event deliverable hanging under a completed
    scheduling-decision child when an existing workflow root is available.
    """

    by_id = {proposal.proposal_id: proposal for proposal in existing_proposals}
    by_id.update({proposal.proposal_id: proposal for proposal in new_proposals})
    updated_existing_by_id: dict[str, Proposal] = {}
    normalized: list[Proposal] = []

    for proposal in new_proposals:
        next_proposal = proposal
        canonical_title = _canonical_workflow_title(proposal, by_id)
        parent_id = parent_proposal_id(next_proposal)
        parent = by_id.get(parent_id) if parent_id else None
        root = _workflow_root(parent, by_id) if parent is not None else _matching_existing_root(next_proposal, by_id)

        if root is not None:
            promoted_root = _promote_root_if_needed(
                root,
                canonical_title=canonical_title,
                normalized_at=normalized_at,
            )
            if promoted_root != root and promoted_root.proposal_id in {item.proposal_id for item in existing_proposals}:
                updated_existing_by_id[promoted_root.proposal_id] = promoted_root
                by_id[promoted_root.proposal_id] = promoted_root
                root = promoted_root

            next_proposal = _normalize_parent_relation(next_proposal, root=root, parent=parent)

        normalized.append(next_proposal)
        by_id[next_proposal.proposal_id] = next_proposal

    return WorkflowGraphNormalization(
        proposals=tuple(normalized),
        updated_existing=tuple(updated_existing_by_id.values()),
    )


def normalize_existing_proposal_graph(
    proposals: tuple[Proposal, ...],
    *,
    normalized_at: datetime,
) -> WorkflowGraphNormalization:
    """Backfill existing lifecycle graphs using the root/reparent policy.

    Existing-state backfill is intentionally more conservative than new-input
    normalization: it may repair an already-linked workflow subtree, but it
    should not infer brand-new parentage for arbitrary old follow-up-looking
    tasks from weak topic-token overlap.  That prevents unrelated historical
    work items with generic words such as "result" or "summary" from being
    swept under a nearby workflow container.
    """

    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    updates: dict[str, Proposal] = {}
    created: dict[str, Proposal] = {}

    for proposal in proposals:
        canonical_title = _canonical_workflow_title(proposal, by_id)
        if not canonical_title and not _is_follow_up_deliverable(proposal):
            continue

        parent_id = parent_proposal_id(proposal)
        parent = by_id.get(parent_id) if parent_id else None
        root = _workflow_root(parent, by_id) if parent is not None else None
        if root is None and canonical_title and _has_children(proposal, by_id):
            root = proposal
        if root is None and canonical_title:
            root = _matching_existing_root(proposal, by_id, require_canonical_title=True)
        if root is None:
            continue

        promoted_root = _promote_root_if_needed(root, canonical_title=canonical_title, normalized_at=normalized_at)
        if promoted_root != root:
            updates[promoted_root.proposal_id] = promoted_root
            by_id[promoted_root.proposal_id] = promoted_root
            root = promoted_root

        if proposal.proposal_id == root.proposal_id:
            continue

        current = updates.get(proposal.proposal_id, proposal)
        normalized = _normalize_parent_relation(current, root=root, parent=parent)
        if normalized != proposal:
            updates[normalized.proposal_id] = normalized
            by_id[normalized.proposal_id] = normalized

    for updated in _normalize_linked_completion_sources(by_id, normalized_at=normalized_at):
        if updated.proposal_id not in created:
            updates[updated.proposal_id] = updated
            by_id[updated.proposal_id] = updated

    for updated in _normalize_multi_parent_dependencies(by_id):
        if updated.proposal_id not in created:
            updates[updated.proposal_id] = updated
            by_id[updated.proposal_id] = updated

    for parent, children in _create_dependency_workflow_containers(by_id, normalized_at=normalized_at):
        if parent.proposal_id not in by_id:
            created[parent.proposal_id] = parent
            by_id[parent.proposal_id] = parent
        for child in children:
            updates[child.proposal_id] = child
            by_id[child.proposal_id] = child

    return WorkflowGraphNormalization(proposals=tuple(created.values()), updated_existing=tuple(updates.values()))


def _normalize_linked_completion_sources(
    by_id: dict[str, Proposal],
    *,
    normalized_at: datetime,
) -> tuple[Proposal, ...]:
    updates: list[Proposal] = []
    for completed in tuple(by_id.values()):
        source_id = completed.metadata.get("linked_completion_source_proposal_id", "").strip()
        if not source_id:
            continue
        source = by_id.get(source_id)
        if source is None or source.proposal_id == completed.proposal_id:
            continue
        root = _workflow_root(completed, by_id)
        if root is None or root.proposal_id == completed.proposal_id:
            continue
        if parent_proposal_id(source) == root.proposal_id:
            normalized = _normalize_parent_relation(source, root=root, parent=root)
        else:
            metadata = dict(source.metadata)
            current_parent_id = parent_proposal_id(source)
            if current_parent_id and current_parent_id != root.proposal_id:
                metadata.setdefault("original_parent_proposal_id", current_parent_id)
                metadata["relation_normalized_from_parent_id"] = current_parent_id
            metadata[PARENT_PROPOSAL_ID_KEY] = root.proposal_id
            metadata[DEPENDS_ON_PROPOSAL_IDS_KEY] = _append_csv_value(
                metadata.get(DEPENDS_ON_PROPOSAL_IDS_KEY, ""),
                completed.proposal_id,
            )
            metadata.setdefault("relation_type", "workflow_completion_evidence")
            metadata["workflow_relation_normalized"] = "true"
            metadata["workflow_completion_source_for_proposal_id"] = completed.proposal_id
            metadata.setdefault("workflow_graph_normalized_at", normalized_at.isoformat(timespec="seconds"))
            normalized = replace(source, metadata=metadata)
            normalized = _normalize_parent_relation(normalized, root=root, parent=root)
        if normalized != source:
            updates.append(normalized)
    return tuple(updates)


def _normalize_multi_parent_dependencies(by_id: dict[str, Proposal]) -> tuple[Proposal, ...]:
    updates: list[Proposal] = []
    for proposal in tuple(by_id.values()):
        parent_raw = proposal.metadata.get(PARENT_PROPOSAL_ID_KEY, "").strip()
        parent_ids = [item.strip() for item in parent_raw.split(",") if item.strip()]
        if len(parent_ids) < 2:
            continue
        metadata = dict(proposal.metadata)
        metadata.setdefault("original_parent_proposal_id", parent_raw)
        metadata["relation_normalized_from_parent_id"] = parent_raw
        for parent_id in parent_ids:
            metadata[DEPENDS_ON_PROPOSAL_IDS_KEY] = _append_csv_value(
                metadata.get(DEPENDS_ON_PROPOSAL_IDS_KEY, ""),
                parent_id,
            )
        roots = {
            root.proposal_id: root
            for parent_id in parent_ids
            if (parent := by_id.get(parent_id)) is not None
            if (root := _workflow_root(parent, by_id)) is not None
        }
        if len(roots) == 1:
            root = next(iter(roots.values()))
            metadata[PARENT_PROPOSAL_ID_KEY] = root.proposal_id
            metadata["relation_type"] = metadata.get("relation_type", "multi_parent_workflow_dependency")
            normalized = _normalize_parent_relation(replace(proposal, metadata=metadata), root=root, parent=root)
        else:
            metadata.pop(PARENT_PROPOSAL_ID_KEY, None)
            metadata["relation_type"] = metadata.get("relation_type", "dependency_only")
            metadata["workflow_relation_normalized"] = "true"
            normalized = replace(proposal, metadata=metadata)
        if normalized != proposal:
            updates.append(normalized)
    return tuple(updates)


def _create_dependency_workflow_containers(
    by_id: dict[str, Proposal],
    *,
    normalized_at: datetime,
) -> tuple[tuple[Proposal, tuple[Proposal, ...]], ...]:
    groups: dict[str, list[Proposal]] = {}
    for proposal in by_id.values():
        workflow_title = proposal.metadata.get(WORKFLOW_TITLE_KEY, "").strip()
        if not workflow_title:
            continue
        if parent_proposal_id(proposal):
            continue
        if proposal.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE:
            continue
        if proposal.status == "rejected":
            continue
        groups.setdefault(_normalize_title(workflow_title), []).append(proposal)

    creations: list[tuple[Proposal, tuple[Proposal, ...]]] = []
    for _, items in groups.items():
        if len(items) < 2:
            continue
        workflow_title = items[0].metadata.get(WORKFLOW_TITLE_KEY, "").strip()
        if not workflow_title:
            continue
        if any(
            candidate.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE
            and _same_normalized_title(candidate.metadata.get(WORKFLOW_TITLE_KEY, candidate.title), workflow_title)
            for candidate in by_id.values()
        ):
            continue
        item_ids = {item.proposal_id for item in items}
        if not any(dep_id in item_ids for item in items for dep_id in depends_on_proposal_ids(item)):
            continue
        parent_id = _synthetic_workflow_parent_id(workflow_title, tuple(item.proposal_id for item in items))
        if parent_id in by_id:
            continue
        ordered = _order_dependency_group(items)
        parent = _synthetic_workflow_parent(parent_id, workflow_title, ordered, normalized_at=normalized_at)
        children: list[Proposal] = []
        for index, child in enumerate(ordered, start=1):
            metadata = dict(child.metadata)
            metadata.setdefault("original_parent_proposal_id", "")
            metadata[PARENT_PROPOSAL_ID_KEY] = parent.proposal_id
            metadata[WORKFLOW_TITLE_KEY] = workflow_title
            metadata[WORKFLOW_ID_KEY] = parent.metadata[WORKFLOW_ID_KEY]
            metadata["workflow_group_id"] = f"workflow-group/{parent.proposal_id}"
            metadata.setdefault(WORKFLOW_ROLE_KEY, "child")
            metadata.setdefault(STEP_INDEX_KEY, str(index))
            metadata.setdefault(STEP_COUNT_KEY, str(len(ordered)))
            metadata.setdefault("relation_type", "workflow_step")
            metadata["workflow_relation_normalized"] = "true"
            children.append(replace(child, metadata=metadata))
        creations.append((parent, tuple(children)))
    return tuple(creations)


def _normalize_parent_relation(proposal: Proposal, *, root: Proposal, parent: Proposal | None) -> Proposal:
    metadata = dict(proposal.metadata)
    current_parent_id = metadata.get(PARENT_PROPOSAL_ID_KEY, "").strip()
    should_attach_to_root = False

    if not current_parent_id:
        should_attach_to_root = _is_follow_up_deliverable(proposal)
    elif parent is not None and parent.proposal_id != root.proposal_id:
        should_attach_to_root = _is_follow_up_deliverable(proposal) and (
            parent.status in {"done", "rejected"} or _is_planning_like(parent)
        )

    if should_attach_to_root:
        if current_parent_id and current_parent_id != root.proposal_id:
            metadata.setdefault("original_parent_proposal_id", current_parent_id)
            metadata["relation_normalized_from_parent_id"] = current_parent_id
            metadata[DEPENDS_ON_PROPOSAL_IDS_KEY] = _append_csv_value(
                metadata.get(DEPENDS_ON_PROPOSAL_IDS_KEY, ""),
                current_parent_id,
            )
        metadata[PARENT_PROPOSAL_ID_KEY] = root.proposal_id
        metadata.setdefault("relation_type", "post_event_followup")
        metadata["workflow_relation_normalized"] = "true"

    if metadata.get(PARENT_PROPOSAL_ID_KEY) == root.proposal_id:
        metadata[WORKFLOW_TITLE_KEY] = root.metadata.get(WORKFLOW_TITLE_KEY, root.title)
        if root.metadata.get(WORKFLOW_ID_KEY):
            metadata[WORKFLOW_ID_KEY] = root.metadata[WORKFLOW_ID_KEY]
        metadata["workflow_group_id"] = f"workflow-group/{root.proposal_id}"

    return replace(proposal, metadata=metadata)


def _promote_root_if_needed(
    root: Proposal,
    *,
    canonical_title: str,
    normalized_at: datetime,
) -> Proposal:
    if not canonical_title:
        return root
    if _same_normalized_title(root.title, canonical_title):
        metadata = _workflow_container_metadata(root, canonical_title=canonical_title, normalized_at=normalized_at)
        return replace(root, metadata=metadata) if metadata != root.metadata else root
    if not (_is_planning_like(root) or root.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE):
        return root

    metadata = _workflow_container_metadata(root, canonical_title=canonical_title, normalized_at=normalized_at)
    metadata.setdefault("previous_title", root.title)
    metadata.setdefault("workflow_original_title", root.title)
    metadata["workflow_renamed_from_title"] = root.title
    metadata["workflow_rename_reason"] = "broader_event_identity_discovered"
    return replace(root, title=canonical_title, metadata=metadata, updated_at=normalized_at)


def _workflow_container_metadata(root: Proposal, *, canonical_title: str, normalized_at: datetime) -> dict[str, str]:
    metadata = dict(root.metadata)
    metadata[WORKFLOW_ROLE_KEY] = WORKFLOW_PARENT_ROLE
    metadata[WORKFLOW_TITLE_KEY] = canonical_title
    metadata.setdefault(WORKFLOW_ID_KEY, _workflow_id_from_title(canonical_title))
    metadata["workflow_container"] = "true"
    metadata["action_required"] = "false"
    metadata["resolution_required"] = "false"
    metadata["workflow_graph_normalized"] = "true"
    metadata.setdefault("workflow_graph_normalized_at", normalized_at.isoformat(timespec="seconds"))
    return metadata


def _workflow_root(proposal: Proposal | None, by_id: dict[str, Proposal]) -> Proposal | None:
    if proposal is None:
        return None
    current = proposal
    seen: set[str] = set()
    while True:
        if current.proposal_id in seen:
            return current
        seen.add(current.proposal_id)
        parent_id = parent_proposal_id(current)
        parent = by_id.get(parent_id) if parent_id else None
        if parent is None:
            return current
        current = parent


def _matching_existing_root(
    proposal: Proposal,
    by_id: dict[str, Proposal],
    *,
    require_canonical_title: bool = False,
) -> Proposal | None:
    if not _is_follow_up_deliverable(proposal):
        return None
    canonical_title = _canonical_workflow_title(proposal, by_id)
    proposal_tokens = _topic_tokens(*_proposal_texts(proposal))
    best: tuple[int, str, Proposal] | None = None
    for candidate in by_id.values():
        if candidate.proposal_id == proposal.proposal_id:
            continue
        root = _workflow_root(candidate, by_id)
        if root is None:
            continue
        children_exist = _has_children(root, by_id)
        if not children_exist and root.metadata.get(WORKFLOW_ROLE_KEY) != WORKFLOW_PARENT_ROLE:
            continue
        root_texts = _subtree_texts(root, by_id)
        score = 0
        if canonical_title and any(_same_normalized_title(canonical_title, text) for text in root_texts):
            score += 100
        elif require_canonical_title:
            continue
        overlap = proposal_tokens & _topic_tokens(*root_texts)
        if overlap:
            score += 10 + len(overlap)
        if root.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE:
            score += 3
        if root.status not in {"done", "rejected"}:
            score += 2
        if score <= 12:
            continue
        contender = (score, root.proposal_id, root)
        if best is None or contender[:2] > best[:2]:
            best = contender
    return best[2] if best is not None else None


def _has_children(proposal: Proposal, by_id: dict[str, Proposal]) -> bool:
    return any(parent_proposal_id(item) == proposal.proposal_id for item in by_id.values())


def _canonical_workflow_title(proposal: Proposal, by_id: dict[str, Proposal]) -> str:
    for text in _proposal_texts(proposal):
        title = _series_title(text)
        if title:
            return title
    parent_id = parent_proposal_id(proposal)
    parent = by_id.get(parent_id) if parent_id else None
    if parent is not None:
        for text in _subtree_texts(_workflow_root(parent, by_id) or parent, by_id):
            title = _series_title(text)
            if title:
                return title
    workflow_title = proposal.metadata.get(WORKFLOW_TITLE_KEY, "").strip()
    return workflow_title


def _series_title(text: str) -> str:
    match = _SERIES_TITLE_RE.search(text)
    if match is None:
        return ""
    episode = match.group(1)
    if len(episode) >= 4:
        return ""
    topic = match.group(2).strip(" -_/·")
    if not topic:
        return ""
    return f"제{match.group(1)}회 {topic}"


def _proposal_texts(proposal: Proposal) -> tuple[str, ...]:
    return tuple(
        item
        for item in (
            proposal.title,
            proposal.raw_text,
            proposal.metadata.get(WORKFLOW_TITLE_KEY, ""),
            proposal.metadata.get("previous_title", ""),
            proposal.metadata.get("workflow_original_title", ""),
        )
        if item
    )


def _subtree_texts(root: Proposal, by_id: dict[str, Proposal]) -> tuple[str, ...]:
    texts: list[str] = []
    queue = [root]
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current.proposal_id in seen:
            continue
        seen.add(current.proposal_id)
        texts.extend(_proposal_texts(current))
        queue.extend(item for item in by_id.values() if parent_proposal_id(item) == current.proposal_id)
    return tuple(texts)


def _is_follow_up_deliverable(proposal: Proposal) -> bool:
    text = " ".join(_proposal_texts(proposal)).lower()
    return any(marker in text for marker in _FOLLOW_UP_MARKERS)


def _is_planning_like(proposal: Proposal) -> bool:
    text = " ".join(_proposal_texts(proposal)).lower()
    return any(marker in text for marker in _PLANNING_MARKERS)


def _topic_tokens(*texts: str) -> set[str]:
    tokens = set()
    for text in texts:
        for token in _TOKEN_RE.findall(text.lower()):
            if token not in _TOPIC_STOPWORDS and len(token) >= 2:
                tokens.add(token)
    return tokens


def _same_normalized_title(left: str, right: str) -> bool:
    return _normalize_title(left) == _normalize_title(right)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _workflow_id_from_title(title: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣._-]+", "-", title.strip().lower()).strip("-")
    return normalized or "workflow"


def _synthetic_workflow_parent_id(title: str, child_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha1(("|".join((title, *sorted(child_ids)))).encode("utf-8")).hexdigest()[:12]
    return f"backfill/workflow/{digest}"


def _synthetic_workflow_parent(
    proposal_id: str,
    title: str,
    children: tuple[Proposal, ...],
    *,
    normalized_at: datetime,
) -> Proposal:
    first = children[0]
    child_dates = [child.due_date or child.scheduled_date for child in children if child.due_date or child.scheduled_date]
    status = "done" if all(child.status in {"done", "applied", "rejected"} for child in children) else "approved"
    created_at = min((child.created_at for child in children if child.created_at), default=normalized_at)
    metadata = _workflow_container_metadata(replace(first, metadata={}), canonical_title=title, normalized_at=normalized_at)
    metadata[WORKFLOW_ID_KEY] = first.metadata.get(WORKFLOW_ID_KEY, _workflow_id_from_title(title))
    metadata["workflow_backfill_created"] = "true"
    metadata["workflow_backfill_child_ids"] = ",".join(child.proposal_id for child in children)
    metadata["workflow_group_id"] = f"workflow-group/{proposal_id}"
    if first.metadata.get("source_channel"):
        metadata["source_channel"] = first.metadata["source_channel"]
    if first.metadata.get("source_provider"):
        metadata["source_provider"] = first.metadata["source_provider"]
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=first.source_message_id,
        proposer_id=first.proposer_id,
        title=title,
        raw_text=title,
        kind="task",
        status=status,
        assigned_to=first.assigned_to,
        task_management_area=first.task_management_area,
        discussion_id=first.discussion_id,
        message_id=first.message_id,
        required_approvers=first.required_approvers,
        approvals=first.approvals if status in {"approved", "done", "applied"} else (),
        due_date=max(child_dates) if child_dates else None,
        created_at=created_at,
        updated_at=normalized_at,
        metadata=metadata,
    )


def _order_dependency_group(proposals: list[Proposal]) -> tuple[Proposal, ...]:
    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    remaining = set(by_id)
    ordered: list[Proposal] = []
    while remaining:
        ready = [
            by_id[proposal_id]
            for proposal_id in remaining
            if not any(dep_id in remaining for dep_id in depends_on_proposal_ids(by_id[proposal_id]))
        ]
        if not ready:
            ready = [by_id[proposal_id] for proposal_id in remaining]
        ready.sort(key=lambda item: (item.due_date or item.scheduled_date or datetime.max.date(), item.time_window, item.title))
        chosen = ready[0]
        ordered.append(chosen)
        remaining.remove(chosen.proposal_id)
    return tuple(ordered)


def _append_csv_value(raw: str, value: str) -> str:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if value not in values:
        values.append(value)
    return ",".join(values)

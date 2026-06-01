from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import sys
from typing import Any, Iterable

from .domain import ADAPTER_SCHEMA, TASK_EXPORT_SCHEMA, TeamTaskTaskCandidate, Proposal
from .relations import (
    WORKFLOW_GROUP_CHILD_IDS_KEY,
    is_workflow_parent,
    parent_proposal_id,
)


DEFAULT_TASK_CORE_PATH = Path.home() / "claudecode" / "llm-wiki"
SYNC_MODEL = "task_management-preview-only-adapter"


def build_task_management_task_export(
    candidates: Iterable[TeamTaskTaskCandidate],
    *,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = (exported_at or datetime.now()).isoformat(timespec="seconds")
    items = [_candidate_to_export_item(candidate, captured_at=timestamp) for candidate in candidates]
    return {
        "schema_version": TASK_EXPORT_SCHEMA,
        "exported_at": timestamp,
        "sync_model": SYNC_MODEL,
        "conflict_policy": "preview-only; task_management adapter never writes task inbox files",
        "items": items,
        "catalogs": {},
        "diagnostics": {
            "adapter_schema": ADAPTER_SCHEMA,
            "candidate_count": len(items),
            "item_count": len(items),
            "mutates_files": False,
            "utf8_required": True,
            "korean_path_supported": True,
        },
    }


def build_task_management_task_export_from_proposals(
    proposals: Iterable[Proposal],
    *,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    proposal_tuple = tuple(proposals)
    skipped_parent_ids = {
        proposal.proposal_id
        for proposal in proposal_tuple
        if _skip_workflow_parent_by_default(proposal, proposal_tuple)
    }
    approved_candidates = [
        _proposal_to_candidate(proposal)
        for proposal in proposal_tuple
        if proposal.status == "approved"
        and proposal.proposal_id not in skipped_parent_ids
    ]
    payload = build_task_management_task_export(approved_candidates, exported_at=exported_at)
    payload["diagnostics"]["approved_proposal_count"] = len(approved_candidates)
    payload["diagnostics"]["workflow_parent_skipped_count"] = len(skipped_parent_ids)
    payload["diagnostics"]["workflow_parent_skipped_ids"] = sorted(skipped_parent_ids)
    return payload


def validate_with_task_core(
    payload: dict[str, Any],
    *,
    task_core_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    ensure_task_core_on_path(task_core_path)
    from pipeline.adapters.task.server import task_import_preview_payload

    preview_root = root if root is not None else resolved_task_core_path(task_core_path)
    return task_import_preview_payload(payload, root=preview_root)


def ensure_task_core_on_path(task_core_path: str | Path | None = None) -> Path:
    path = resolved_task_core_path(task_core_path)
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)
    return path


def resolved_task_core_path(task_core_path: str | Path | None = None) -> Path:
    if task_core_path is not None:
        return Path(task_core_path)
    return Path(os.environ.get("TASK_CORE_PATH", str(DEFAULT_TASK_CORE_PATH)))


def _candidate_to_export_item(candidate: TeamTaskTaskCandidate, *, captured_at: str) -> dict[str, Any]:
    projection = _project_capture(candidate.title)
    capture_id = candidate.source_key
    task_status = candidate.task_status
    board = _board_for(candidate, task_status=task_status)
    metadata = {
        **candidate.metadata,
        "capture_id": capture_id,
        "source_adapter": "task_management",
        "source_adapter_schema": ADAPTER_SCHEMA,
        "task_management_area": candidate.task_management_area,
        "assigned_to": candidate.assigned_to,
        "discussion_id": candidate.discussion_id,
        "message_id": candidate.message_id,
        "source_text_hash": _text_hash(candidate.raw_text),
        "task_status": task_status,
        "item_type": candidate.item_type,
        "disposition": candidate.disposition,
        "needs_review": str(candidate.needs_review).lower(),
    }
    if candidate.due_date is not None:
        metadata["hard_due_at"] = candidate.due_date.isoformat()
    if candidate.scheduled_date is not None:
        metadata["scheduled_for"] = candidate.scheduled_date.isoformat()
    if candidate.time_window:
        metadata["time_window"] = candidate.time_window
    if candidate.source_url:
        metadata["source_url"] = candidate.source_url
    if candidate.source_export_path:
        metadata["source_export_path"] = candidate.source_export_path
    if candidate.speaker:
        metadata["speaker"] = candidate.speaker

    captured_date = None
    if candidate.due_date is not None:
        captured_date = candidate.due_date.isoformat()
    elif candidate.scheduled_date is not None:
        captured_date = candidate.scheduled_date.isoformat()
    else:
        captured_date = projection.captured_date

    item_type = candidate.item_type or projection.item_type
    disposition = candidate.disposition or projection.disposition
    missing_slots = _missing_slots_for_export(candidate, projection_missing_slots=projection.missing_slots)
    return {
        "id": capture_id,
        "raw_text": candidate.raw_text,
        "captured_at": captured_at,
        "source_channel": "task_management-discussion",
        "task_status": task_status,
        "board": board,
        "disposition": disposition,
        "item_type": item_type,
        "title": candidate.title or projection.title,
        "captured_date": captured_date,
        "missing_slots": missing_slots,
        "metadata": metadata,
        "document_id": f"discussion/{candidate.discussion_id.replace('/', '-')}",
        "path": "manual",
        "line_number": candidate.line_number,
    }


def _proposal_to_candidate(proposal: Proposal) -> TeamTaskTaskCandidate:
    metadata = {
        **proposal.metadata,
        "proposal_id": proposal.proposal_id,
        "proposer_id": proposal.proposer_id,
        "required_approvers": ",".join(proposal.required_approvers),
        "approvals": ",".join(proposal.approvals),
        "source_message_id": proposal.source_message_id,
        "proposal_status": proposal.status,
    }
    return TeamTaskTaskCandidate(
        source_key=proposal.proposal_id,
        raw_text=proposal.raw_text,
        title=proposal.title,
        discussion_id=proposal.discussion_id,
        message_id=proposal.message_id,
        line_number=1,
        assigned_to=proposal.assigned_to,
        task_management_area=proposal.task_management_area,
        due_date=proposal.due_date,
        scheduled_date=proposal.scheduled_date,
        time_window=proposal.time_window,
        task_status="active",
        item_type=proposal.kind if proposal.kind in {"reference", "event", "routine"} else "task",
        disposition="reference" if proposal.kind == "reference" else "execution",
        needs_review=bool(proposal.missing_slots),
        source_url=proposal.source_url,
        source_export_path=proposal.source_export_path,
        metadata=metadata,
    )


def _skip_workflow_parent_by_default(proposal: Proposal, proposals: tuple[Proposal, ...]) -> bool:
    if proposal.metadata.get("export_policy") == "include_parent_and_children":
        return False
    if proposal.metadata.get(WORKFLOW_GROUP_CHILD_IDS_KEY):
        return True
    if is_workflow_parent(proposal, proposals):
        return any(parent_proposal_id(item) == proposal.proposal_id for item in proposals)
    return False


def _project_capture(text: str) -> Any:
    ensure_task_core_on_path()
    try:
        from pipeline.task_core import project_capture
    except ImportError:
        from pipeline.adapters.task.server import project_capture

    return project_capture(text)


def _board_for(candidate: TeamTaskTaskCandidate, *, task_status: str) -> str:
    if task_status == "done":
        return "done"
    if candidate.item_type == "reference" or candidate.disposition == "reference":
        return "reference"
    if candidate.needs_review:
        return "questions"
    return "todo"


def _missing_slots_for_export(
    candidate: TeamTaskTaskCandidate,
    *,
    projection_missing_slots: Iterable[str],
) -> list[str]:
    missing = list(projection_missing_slots)
    if candidate.metadata.get("needs_exact_date") == "true" and "exact_date" not in missing:
        missing.append("exact_date")
    return missing


def _text_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
